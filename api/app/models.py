"""API schemas."""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class BatchStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchInfo(BaseModel):
    batch_id: str
    status: BatchStatus
    total_rows: int = Field(description="Data rows in the uploaded CSV (duplicates included)")
    unique_genes: int = Field(description="Distinct version-stripped identifiers being looked up")
    resolved_genes: int = Field(description="Unique genes whose lookup has finished")
    matched_genes: int
    unmatched_genes: int
    error: str | None = None
    download_url: str | None = Field(default=None, description="Present once status is 'completed'")
    created_at: float


class InflightQuery(BaseModel):
    gene: str
    subscribers: int
    subscriber_batch_ids: list[str]
    task_done: bool | None
