"""Coalescing manager for per-gene annotation lookups.

Guarantees provided to the rest of the application:

* At most one HTTP request per gene is in flight at any moment.  Every
  batch subscribed to that gene receives the result (fan-out).
* A batch that is cancelled is detached immediately; when the *last*
  subscriber of a query goes away, the underlying HTTP request is aborted.
* No caching: once a query finishes (success, 404, error or abort) it is
  forgotten.  The next subscriber for the same gene triggers a brand-new
  HTTP request.
* Late results and cleanup of an aborted query can never interfere with a
  newer query for the same gene: registry entries are compared by object
  identity, and results are only ever delivered to the futures that were
  created for the current subscribers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx


class SourceError(Exception):
    """The annotation source failed in a way that must fail the whole batch.

    A 404 from the source is *not* an error (the gene is simply unmatched);
    anything else (5xx, timeout, connection error, invalid payload) is.
    """


@dataclass(frozen=True)
class Annotation:
    symbol: str
    chromosome: str


@dataclass
class _SharedQuery:
    """One in-flight lookup.  A new object is created for every new query,

    so an aborted/finished query can never be mistaken for a fresh one."""

    gene: str
    task: asyncio.Task | None = None
    subscribers: dict[str, asyncio.Future] = field(default_factory=dict)


class SharedQueryManager:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        max_concurrent: int = 32,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, transport=transport
        )
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._inflight: dict[str, _SharedQuery] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # subscription handling
    # ------------------------------------------------------------------

    async def subscribe_many(self, genes: list[str], batch_id: str) -> dict[str, asyncio.Future]:
        """Attach ``batch_id`` to the shared query of every gene.

        Genes that nobody is currently querying get a fresh HTTP request;
        genes already in flight are simply shared.
        """
        futures: dict[str, asyncio.Future] = {}
        async with self._lock:
            for gene in genes:
                query = self._inflight.get(gene)
                if query is None:
                    query = _SharedQuery(gene=gene)
                    query.task = asyncio.create_task(self._run(query), name=f"query:{gene}")
                    self._inflight[gene] = query
                fut: asyncio.Future = asyncio.get_running_loop().create_future()
                query.subscribers[batch_id] = fut
                futures[gene] = fut
        return futures

    async def unsubscribe_many(self, genes: list[str], batch_id: str) -> None:
        """Detach ``batch_id`` from every gene; abort queries nobody needs."""
        async with self._lock:
            for gene in genes:
                query = self._inflight.get(gene)
                if query is None:
                    continue
                fut = query.subscribers.pop(batch_id, None)
                if fut is not None and not fut.done():
                    fut.cancel()
                if not query.subscribers:
                    # Last interested batch left -> abort the HTTP request and
                    # forget the query.  If the task is already finished this
                    # cancel() is a harmless no-op.
                    del self._inflight[gene]
                    if query.task is not None:
                        query.task.cancel()

    async def snapshot(self) -> list[dict]:
        """Observability: which genes are currently being queried, for whom."""
        async with self._lock:
            return [
                {
                    "gene": q.gene,
                    "subscribers": len(q.subscribers),
                    "subscriber_batch_ids": sorted(q.subscribers),
                    "task_done": q.task.done() if q.task else None,
                }
                for q in self._inflight.values()
            ]

    # ------------------------------------------------------------------
    # query execution
    # ------------------------------------------------------------------

    async def _run(self, query: _SharedQuery) -> None:
        try:
            async with self._semaphore:
                result = await self._fetch(query.gene)
        except asyncio.CancelledError:
            # Aborted because the last subscriber left.  unsubscribe_many()
            # has already removed us from the registry, and any registry
            # entry that exists now for this gene belongs to a *newer*
            # query object -- we must not touch it.  Nothing else to do:
            # results are never cached, so a late response goes nowhere.
            raise
        except Exception as exc:  # delivered to every current subscriber
            self._finish(query, error=exc)
        else:
            self._finish(query, result=result)

    def _finish(self, query: _SharedQuery, *, result=None, error: Exception | None = None) -> None:
        # Called synchronously from the query task: no other coroutine can
        # interleave between the identity check and the fan-out below.
        if self._inflight.get(query.gene) is query:
            del self._inflight[query.gene]
        subscribers = list(query.subscribers.values())
        query.subscribers.clear()
        for fut in subscribers:
            if fut.done():  # batch was cancelled in the meantime
                continue
            if error is None:
                fut.set_result(result)
            else:
                fut.set_exception(error)

    async def _fetch(self, gene: str) -> Annotation | None:
        """One HTTP round-trip.  Returns None for 404 (unmatched gene)."""
        try:
            response = await self._client.get(f"/annotations/{gene}")
        except Exception as exc:  # timeout, connection refused, DNS, ...
            raise SourceError(f"annotation source request for {gene!r} failed: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SourceError(
                f"annotation source returned HTTP {response.status_code} for {gene!r}"
            )
        try:
            payload = response.json()
            return Annotation(symbol=str(payload["symbol"]), chromosome=str(payload["chromosome"]))
        except Exception as exc:
            raise SourceError(f"invalid annotation payload for {gene!r}: {exc}") from exc
