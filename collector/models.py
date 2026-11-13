"""Pydantic schemas for the public API."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Accepted-at-inception terminal/non-terminal job states.
# "cancelling" means cancellation was accepted and browser/teardown work is
# still in flight; the terminal "cancelled" is only reported after cleanup.
JobStatus = Literal[
    "pending",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "timeout",
]

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_STATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class CollectRequest(BaseModel):
    station: str = Field(..., description="Monitoring station code, e.g. ST001")
    month: str = Field(..., description="UTC month in YYYY-MM format")

    @field_validator("station")
    @classmethod
    def _station(cls, v: str) -> str:
        v = v.strip()
        if not _STATION_RE.match(v):
            raise ValueError(
                "station must be 1-32 chars of letters, digits, '_' or '-'"
            )
        return v

    @field_validator("month")
    @classmethod
    def _month(cls, v: str) -> str:
        v = v.strip()
        if not _MONTH_RE.match(v):
            raise ValueError("month must use YYYY-MM format, e.g. 2026-10")
        return v


class CollectAccepted(BaseModel):
    job_id: str
    status: JobStatus
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    station: str
    month: str
    status: JobStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    deadline_at: str
    error: str | None = None
    bytes: int | None = None
    rows: int | None = None
    download_url: str | None = None


class JobSummary(BaseModel):
    job_id: str
    station: str
    month: str
    status: JobStatus
    created_at: str


class CancelResponse(BaseModel):
    job_id: str
    status: JobStatus
    cancelled: bool
    message: str
