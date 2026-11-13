"""Gene Annotation Alignment API.

Upload a CSV with a single ``gene_id`` column (Ensembl ENSG identifiers,
optionally carrying a ``.<digits>`` version suffix as exported by
quantification software).  The batch is annotated asynchronously against a
fixed human gene annotation release: every row gets the standard gene symbol
and chromosome while the original identifier, the original row order and
duplicate rows are preserved.  Lookups for the same gene are coalesced
across concurrent batches; batches can be polled for progress and cancelled
while running.  All state lives in memory only.
"""

from __future__ import annotations

import csv
import io
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from .batches import Batch, BatchStore
from .manager import SharedQueryManager
from .models import BatchInfo, BatchStatus, InflightQuery

SOURCE_URL = os.getenv("ANNOTATION_SOURCE_URL", "http://localhost:8001")
SOURCE_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT_SECONDS", "30"))
MAX_CONCURRENT_QUERIES = int(os.getenv("MAX_CONCURRENT_QUERIES", "32"))

manager = SharedQueryManager(
    SOURCE_URL, timeout=SOURCE_TIMEOUT, max_concurrent=MAX_CONCURRENT_QUERIES
)
store = BatchStore(manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await manager.aclose()


app = FastAPI(
    title="Gene Annotation Alignment API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Aligns ENSG identifiers exported by sequencing software against a fixed "
        "human gene annotation release.\n\n"
        "* `POST /batches` accepts a CSV containing only a `gene_id` column and "
        "starts an asynchronous annotation batch.\n"
        "* Trailing `.<digits>` version suffixes are stripped for the lookup; the "
        "original identifiers are kept verbatim in the output.\n"
        "* A 404 from the annotation source marks the gene as unmatched (`NA`); "
        "any other source failure fails the whole batch and no download is offered.\n"
        "* Concurrent batches share one in-flight HTTP lookup per gene; cancelling "
        "a batch detaches it immediately and aborts lookups nobody else needs.\n"
        "* Finished lookups are never cached; all state is in-memory."
    ),
)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def to_info(batch: Batch) -> BatchInfo:
    return BatchInfo(
        batch_id=batch.id,
        status=batch.status,
        total_rows=len(batch.rows),
        unique_genes=len(batch.genes),
        resolved_genes=batch.resolved_genes,
        matched_genes=batch.matched_genes,
        unmatched_genes=batch.unmatched_genes,
        error=batch.error,
        download_url=(
            f"/batches/{batch.id}/download"
            if batch.status is BatchStatus.COMPLETED
            else None
        ),
        created_at=batch.created_at,
    )


def get_batch_or_404(batch_id: str) -> Batch:
    batch = store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"unknown batch: {batch_id}")
    return batch


def parse_gene_csv(data: bytes) -> list[str]:
    """Validate the upload and return the raw gene_id cells in file order."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, f"CSV must be UTF-8 encoded: {exc}") from exc
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise HTTPException(422, "CSV is empty; expected a header row 'gene_id'")
    header = [cell.strip().lower() for cell in rows[0]]
    if header != ["gene_id"]:
        raise HTTPException(
            422,
            "CSV must contain exactly one column named 'gene_id' "
            f"(got header: {rows[0]!r})",
        )
    values: list[str] = []
    for line, row in enumerate(rows[1:], start=2):
        if len(row) != 1:
            raise HTTPException(
                422, f"line {line}: expected exactly 1 column, got {len(row)}"
            )
        values.append(row[0])
    return values


# ----------------------------------------------------------------------
# endpoints
# ----------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "gene-annotation-alignment-api",
        "docs": "/docs",
        "batches": "/batches",
        "inflight_queries": "/debug/inflight",
    }


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


@app.post(
    "/batches",
    response_model=BatchInfo,
    status_code=202,
    summary="Upload a gene_id CSV and start an annotation batch",
    description=(
        "Accepts either `multipart/form-data` (file field `file`, e.g. from the "
        "Swagger UI) or a raw `text/csv` request body. The CSV must contain "
        "exactly one column named `gene_id`. Processing starts immediately and "
        "is asynchronous: poll `GET /batches/{batch_id}` for progress."
    ),
)
async def create_batch(request: Request, file: UploadFile | None = File(default=None)):
    data = await file.read() if file is not None else await request.body()
    if not data:
        raise HTTPException(422, "empty upload: provide a CSV file or body")
    rows = parse_gene_csv(data)
    batch = store.create(rows)
    return to_info(batch)


@app.get("/batches", response_model=list[BatchInfo], summary="List all batches")
async def list_batches():
    return [to_info(batch) for batch in store.list()]


@app.get(
    "/batches/{batch_id}",
    response_model=BatchInfo,
    summary="Batch status and progress",
)
async def get_batch(batch_id: str):
    return to_info(get_batch_or_404(batch_id))


@app.post(
    "/batches/{batch_id}/cancel",
    response_model=BatchInfo,
    summary="Cancel a running batch (immediate effect)",
    description=(
        "Cancellation takes effect immediately: the batch stops waiting for its "
        "lookups. Lookups that other batches still need continue unaffected; "
        "lookups nobody needs any more are aborted. A cancelled batch stays "
        "cancelled and offers no download."
    ),
)
async def cancel_batch(batch_id: str):
    batch = get_batch_or_404(batch_id)
    if batch.status is not BatchStatus.RUNNING:
        raise HTTPException(
            409, f"batch is {batch.status.value}; only running batches can be cancelled"
        )
    await store.cancel(batch)
    return to_info(batch)


@app.get(
    "/batches/{batch_id}/download",
    summary="Download the annotated CSV (completed batches only)",
    description=(
        "Returns `gene_id,symbol,chromosome` with the original identifiers, row "
        "order and duplicates. Unmatched genes (source 404) are marked `NA`. "
        "Failed or cancelled batches have no download."
    ),
)
async def download_batch(batch_id: str):
    batch = get_batch_or_404(batch_id)
    if batch.status is BatchStatus.RUNNING:
        raise HTTPException(409, "batch is still running")
    if batch.status is BatchStatus.FAILED:
        raise HTTPException(
            409, f"batch failed ({batch.error}); no download available"
        )
    if batch.status is BatchStatus.CANCELLED:
        raise HTTPException(409, "batch was cancelled; no download available")
    return Response(
        content=batch.result_csv,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="annotated_{batch.id}.csv"'
        },
    )


@app.get(
    "/debug/inflight",
    response_model=list[InflightQuery],
    summary="Currently running shared gene lookups (observability)",
    description=(
        "Shows which genes are being queried right now and which batches share "
        "each lookup. Useful to verify that concurrent batches coalesce "
        "identical genes into a single HTTP request."
    ),
)
async def inflight_queries():
    return await manager.snapshot()
