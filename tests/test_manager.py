"""Unit tests for the coalescing query manager and batch helpers.

Run from the repository root:  python3 -m pytest tests/ -q
(no pytest-asyncio needed; every async test drives its own event loop)
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app.batches import Batch, render_csv, strip_version  # noqa: E402
from app.manager import Annotation, SharedQueryManager, SourceError  # noqa: E402


class FakeManager(SharedQueryManager):
    """Manager whose ``_fetch`` is fully controllable: it records every call,

    blocks until ``release`` is set, then returns ``result`` or raises ``error``."""

    def __init__(self, result=None, error: Exception | None = None):
        super().__init__("http://fake-source")
        self.result = result
        self.error = error
        self.calls: list[str] = []
        self.first_call_started = asyncio.Event()
        self.release = asyncio.Event()

    async def _fetch(self, gene: str):
        self.calls.append(gene)
        self.first_call_started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.result


def test_concurrent_subscribers_share_one_fetch():
    async def main():
        m = FakeManager(result=Annotation("TP53", "17"))
        fa = await m.subscribe_many(["G1"], "batch-a")
        fb = await m.subscribe_many(["G1"], "batch-b")
        await m.first_call_started.wait()
        assert m.calls == ["G1"]  # one HTTP request for both batches
        m.release.set()
        assert await fa["G1"] == Annotation("TP53", "17")
        assert await fb["G1"] == Annotation("TP53", "17")
        assert m.calls == ["G1"]
        assert await m.snapshot() == []  # forgotten once finished
        await m.aclose()

    asyncio.run(main())


def test_unsubscribing_last_subscriber_aborts_and_next_batch_refetches():
    async def main():
        m = FakeManager(result=Annotation("TP53", "17"))
        f1 = await m.subscribe_many(["G1"], "batch-1")
        await m.first_call_started.wait()
        await m.unsubscribe_many(["G1"], "batch-1")
        assert f1["G1"].cancelled()
        assert await m.snapshot() == []  # query forgotten immediately
        await asyncio.sleep(0.05)  # let the aborted task finish dying

        m.first_call_started.clear()
        f2 = await m.subscribe_many(["G1"], "batch-2")
        await m.first_call_started.wait()
        assert m.calls == ["G1", "G1"]  # brand-new HTTP request, no caching
        m.release.set()
        assert await f2["G1"] == Annotation("TP53", "17")
        await m.aclose()

    asyncio.run(main())


def test_remaining_subscriber_unaffected_when_other_cancels():
    async def main():
        m = FakeManager(result=Annotation("BRCA1", "17"))
        fa = await m.subscribe_many(["G1"], "batch-a")
        fb = await m.subscribe_many(["G1"], "batch-b")
        await m.first_call_started.wait()
        await m.unsubscribe_many(["G1"], "batch-b")
        assert fb["G1"].cancelled()
        snapshot = await m.snapshot()
        assert len(snapshot) == 1 and snapshot[0]["subscribers"] == 1  # query survives
        m.release.set()
        assert await fa["G1"] == Annotation("BRCA1", "17")
        await m.aclose()

    asyncio.run(main())


def test_no_cache_after_completion():
    async def main():
        m = FakeManager(result=Annotation("TP53", "17"))
        m.release.set()
        f1 = await m.subscribe_many(["G1"], "batch-1")
        await f1["G1"]
        f2 = await m.subscribe_many(["G1"], "batch-2")
        await f2["G1"]
        assert m.calls == ["G1", "G1"]  # finished queries are not cached
        await m.aclose()

    asyncio.run(main())


def test_late_finish_of_aborted_query_does_not_disturb_new_query():
    async def main():
        m = FakeManager(result=Annotation("FRESH", "2"))
        await m.subscribe_many(["G1"], "batch-1")
        await m.first_call_started.wait()
        old_query = m._inflight["G1"]
        await m.unsubscribe_many(["G1"], "batch-1")  # aborts old_query's task

        f2 = await m.subscribe_many(["G1"], "batch-2")
        new_query = m._inflight["G1"]
        assert new_query is not old_query

        # The aborted request's response arrives late: it must neither remove
        # the new registry entry nor deliver anything to the new batch.
        m._finish(old_query, result=Annotation("STALE", "9"))
        assert m._inflight["G1"] is new_query
        assert not f2["G1"].done()

        m.release.set()
        assert (await f2["G1"]).symbol == "FRESH"
        await m.aclose()

    asyncio.run(main())


def test_source_error_fans_out_to_every_subscriber():
    async def main():
        m = FakeManager(error=SourceError("boom"))
        m.release.set()
        fa = await m.subscribe_many(["G1"], "batch-a")
        fb = await m.subscribe_many(["G1"], "batch-b")
        for fut in (fa["G1"], fb["G1"]):
            with pytest.raises(SourceError, match="boom"):
                await fut
        assert await m.snapshot() == []
        await m.aclose()

    asyncio.run(main())


def test_fetch_maps_http_statuses():
    async def main():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/KNOWN"):
                return httpx.Response(200, json={"symbol": "TP53", "chromosome": "17"})
            if request.url.path.endswith("/BROKEN"):
                return httpx.Response(500, json={"detail": "server exploded"})
            return httpx.Response(404, json={"detail": "no such gene"})

        m = SharedQueryManager("http://fake", transport=httpx.MockTransport(handler))
        assert await m._fetch("KNOWN") == Annotation("TP53", "17")
        assert await m._fetch("UNKNOWN") is None  # 404 -> unmatched, not an error
        with pytest.raises(SourceError):
            await m._fetch("BROKEN")
        await m.aclose()

    asyncio.run(main())


def test_strip_version_removes_only_trailing_dot_digits():
    assert strip_version("ENSG00000141510.17") == "ENSG00000141510"
    assert strip_version("ENSG00000141510") == "ENSG00000141510"
    assert strip_version("  ENSG00000141510.3  ") == "ENSG00000141510"
    assert strip_version("ENSG00000141510.") == "ENSG00000141510."  # no digits: untouched
    assert strip_version("ENSG00000141510.17.2") == "ENSG00000141510.17"
    assert strip_version("") == ""


def test_render_csv_keeps_order_duplicates_and_marks_unmatched():
    batch = Batch(
        id="t1",
        rows=["ENSG00000141510.17", "ENSG00000999999.1", "ENSG00000141510.17"],
        lookup_keys=["ENSG00000141510", "ENSG00000999999", "ENSG00000141510"],
        genes=["ENSG00000141510", "ENSG00000999999"],
    )
    csv_text = render_csv(
        batch, {"ENSG00000141510": Annotation("TP53", "17"), "ENSG00000999999": None}
    )
    assert csv_text.splitlines() == [
        "gene_id,symbol,chromosome",
        "ENSG00000141510.17,TP53,17",
        "ENSG00000999999.1,NA,NA",
        "ENSG00000141510.17,TP53,17",
    ]
