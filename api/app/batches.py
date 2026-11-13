"""Batch lifecycle: CSV rows in, annotated CSV out, all state in memory."""

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import uuid
from dataclasses import dataclass, field

from .manager import Annotation, SharedQueryManager, SourceError
from .models import BatchStatus

# Trailing Ensembl version suffix, e.g. the ".17" in "ENSG00000141510.17".
_VERSION_SUFFIX = re.compile(r"\.\d+$")

# Marker written into symbol/chromosome for genes the source does not know
# (HTTP 404).  "NA" is the conventional missing-value marker in bioinformatics.
UNMATCHED_MARKER = "NA"


def strip_version(gene_id: str) -> str:
    """Lookup key: identifier without a trailing ``.<digits>`` version suffix."""
    return _VERSION_SUFFIX.sub("", gene_id.strip())


@dataclass
class Batch:
    id: str
    rows: list[str]  # original gene_id cells, exactly as uploaded (order kept)
    lookup_keys: list[str]  # per-row version-stripped lookup keys
    genes: list[str]  # unique non-empty lookup keys, first-seen order
    status: BatchStatus = BatchStatus.RUNNING
    resolved_genes: int = 0
    matched_genes: int = 0
    unmatched_genes: int = 0
    error: str | None = None
    result_csv: str | None = None
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None


def render_csv(batch: Batch, results: dict[str, Annotation | None]) -> str:
    """Output CSV: original identifiers, original row order, duplicates kept."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["gene_id", "symbol", "chromosome"])
    for raw, key in zip(batch.rows, batch.lookup_keys):
        annotation = results.get(key) if key else None
        if annotation is None:
            writer.writerow([raw, UNMATCHED_MARKER, UNMATCHED_MARKER])
        else:
            writer.writerow([raw, annotation.symbol, annotation.chromosome])
    return buffer.getvalue()


class BatchStore:
    """In-memory batch registry.  Nothing is persisted; a restart loses all
    batches, and finished gene lookups are never cached."""

    def __init__(self, manager: SharedQueryManager):
        self._manager = manager
        self._batches: dict[str, Batch] = {}

    def create(self, rows: list[str]) -> Batch:
        lookup_keys = [strip_version(row) for row in rows]
        genes = list(dict.fromkeys(key for key in lookup_keys if key))
        batch = Batch(
            id=uuid.uuid4().hex[:12],
            rows=rows,
            lookup_keys=lookup_keys,
            genes=genes,
        )
        self._batches[batch.id] = batch
        batch.task = asyncio.create_task(self._run(batch), name=f"batch:{batch.id}")
        return batch

    def get(self, batch_id: str) -> Batch | None:
        return self._batches.get(batch_id)

    def list(self) -> list[Batch]:
        return sorted(self._batches.values(), key=lambda b: b.created_at)

    async def cancel(self, batch: Batch) -> None:
        """Cancel a running batch with immediate effect.

        The batch is marked cancelled synchronously and detached from every
        shared query *before* this method returns, so a batch submitted
        right afterwards will never join a doomed query.  Queries that other
        batches still need keep running; queries nobody needs are aborted.
        """
        batch.status = BatchStatus.CANCELLED
        await self._manager.unsubscribe_many(batch.genes, batch.id)
        if batch.task is not None:
            batch.task.cancel()

    async def _run(self, batch: Batch) -> None:
        futures = await self._manager.subscribe_many(batch.genes, batch.id)
        results: dict[str, Annotation | None] = {key: None for key in batch.lookup_keys}
        try:
            # The HTTP requests all run concurrently inside the manager; the
            # loop below only collects their outcomes and tracks progress.
            for gene, fut in futures.items():
                results[gene] = await fut
                batch.resolved_genes += 1
                if results[gene] is None:
                    batch.unmatched_genes += 1
                else:
                    batch.matched_genes += 1
        except asyncio.CancelledError:
            batch.status = BatchStatus.CANCELLED
            raise
        except SourceError as exc:
            # Any non-404 source failure fails the whole batch; no download.
            batch.status = BatchStatus.FAILED
            batch.error = str(exc)
        except Exception as exc:  # defensive: never leave a batch stuck
            batch.status = BatchStatus.FAILED
            batch.error = f"unexpected error: {exc}"
        else:
            batch.result_csv = render_csv(batch, results)
            batch.status = BatchStatus.COMPLETED
        finally:
            # Idempotent detach (cancel() may already have done it) and
            # retrieval of exceptions nobody awaited, to keep the asyncio
            # "exception was never retrieved" warnings away.
            await self._manager.unsubscribe_many(batch.genes, batch.id)
            for fut in futures.values():
                if fut.done() and not fut.cancelled():
                    fut.exception()
