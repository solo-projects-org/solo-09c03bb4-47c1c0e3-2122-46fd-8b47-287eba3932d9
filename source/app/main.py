"""Local stand-in for a slow external gene annotation source.

Behaviour contract (mirrors what the alignment API expects):

* ``GET /annotations/{gene_id}`` answers after a configurable delay with
  ``{"gene_id", "symbol", "chromosome"}`` for known genes, **404** for
  unknown genes, and **500** for any identifier containing ``FAIL`` (to
  demonstrate whole-batch failure on non-404 source errors).
* ``GET /stats`` exposes how many HTTP requests were received per gene, so
  the request-coalescing of the API can be demonstrated and verified.
"""

from __future__ import annotations

import asyncio
import collections
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .genes import ANNOTATION_RELEASE, ANNOTATIONS

DELAY = float(os.getenv("ANNOTATION_DELAY_SECONDS", "2.0"))

app = FastAPI(
    title="Local Gene Annotation Source",
    version="1.0.0",
    description=(
        "Simulates a slow Ensembl-like annotation lookup service backed by a "
        "fixed human gene annotation release. Every lookup takes "
        f"{DELAY}s. `/stats` counts received requests per gene."
    ),
)

_requests_per_gene: collections.Counter[str] = collections.Counter()
_in_flight = 0


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "local-gene-annotation-source",
        "annotation_release": ANNOTATION_RELEASE,
        "delay_seconds": DELAY,
        "known_genes": len(ANNOTATIONS),
        "docs": "/docs",
    }


@app.get("/annotations/{gene_id}")
async def get_annotation(gene_id: str):
    global _in_flight
    _requests_per_gene[gene_id] += 1
    _in_flight += 1
    try:
        await asyncio.sleep(DELAY)
        if "FAIL" in gene_id.upper():
            return JSONResponse(
                status_code=500,
                content={"detail": f"simulated upstream failure for {gene_id!r}"},
            )
        annotation = ANNOTATIONS.get(gene_id)
        if annotation is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"no annotation found for {gene_id!r}"},
            )
        return {
            "gene_id": gene_id,
            "symbol": annotation["symbol"],
            "chromosome": annotation["chromosome"],
            "annotation_release": ANNOTATION_RELEASE,
        }
    finally:
        _in_flight -= 1


@app.get("/stats")
async def stats():
    return {
        "annotation_release": ANNOTATION_RELEASE,
        "delay_seconds": DELAY,
        "total_requests": sum(_requests_per_gene.values()),
        "requests_per_gene": dict(sorted(_requests_per_gene.items())),
        "in_flight": _in_flight,
    }


@app.post("/stats/reset")
async def reset_stats():
    _requests_per_gene.clear()
    return {"ok": True}
