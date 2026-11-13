"""FastAPI collection service.

Endpoints
---------
POST   /collect/jobs              accept a collection job (202 + job id)
GET    /collect/jobs              list jobs
GET    /collect/jobs/{job_id}     job status
POST   /collect/jobs/{job_id}/cancel
GET    /collect/jobs/{job_id}/download   only available after validation
GET    /healthz                   liveness probe

The portal URL is fixed configuration (see config.py); clients only supply
a station code and a UTC month.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .config import settings
from .manager import JobManager, JobRecord
from .models import (
    CancelResponse,
    CollectAccepted,
    CollectRequest,
    JobStatusResponse,
    JobSummary,
)

app = FastAPI(
    title="空气质量月度读数采集 API",
    description=(
        "通过真实浏览器（Playwright）操作旧监测门户，选站、查询并点击"
        "导出 CSV。任务之间浏览器会话、窗口与下载完全隔离；仅当文件"
        "完整下载且每行站号、UTC 月份校验通过后才开放下载。"
    ),
    version="1.0.0",
)
manager = JobManager(settings)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await manager.aclose()


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _status_response(record: JobRecord) -> JobStatusResponse:
    download_url = None
    if record.status == "succeeded" and record.download_path is not None:
        download_url = f"/collect/jobs/{record.job_id}/download"
    return JobStatusResponse(
        job_id=record.job_id,
        station=record.station,
        month=record.month,
        status=record.status,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        deadline_at=record.deadline_at,
        error=record.error,
        bytes=record.size_bytes,
        rows=record.rows,
        download_url=download_url,
    )


@app.post(
    "/collect/jobs",
    response_model=CollectAccepted,
    status_code=202,
    tags=["jobs"],
    summary="受理采集任务",
)
async def submit_job(req: CollectRequest) -> CollectAccepted:
    record = await manager.submit(req.station, req.month)
    return CollectAccepted(
        job_id=record.job_id,
        status=record.status,
        message=(
            f"job accepted; {settings.job_timeout_seconds:.0f}s budget from "
            "acceptance; poll status or cancel via the links below"
        ),
    )


@app.get(
    "/collect/jobs",
    response_model=list[JobSummary],
    tags=["jobs"],
    summary="列出全部任务",
)
async def list_jobs() -> list[JobSummary]:
    return [
        JobSummary(
            job_id=r.job_id,
            station=r.station,
            month=r.month,
            status=r.status,
            created_at=r.created_at,
        )
        for r in manager.list_jobs()
    ]


@app.get(
    "/collect/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["jobs"],
    summary="查询任务状态",
)
async def job_status(job_id: str) -> JobStatusResponse:
    record = manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _status_response(record)


@app.post(
    "/collect/jobs/{job_id}/cancel",
    response_model=CancelResponse,
    tags=["jobs"],
    summary="取消任务",
)
async def cancel_job(job_id: str) -> CancelResponse:
    accepted, message = await manager.request_cancel(job_id)
    record = manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not accepted:
        # Idempotent no-op for already-terminal jobs: 409 explains why.
        raise HTTPException(status_code=409, detail=message)
    return CancelResponse(
        job_id=job_id,
        status=record.status,
        cancelled=True,
        message=message,
    )


@app.get(
    "/collect/jobs/{job_id}/download",
    tags=["jobs"],
    summary="下载已校验的 CSV（仅成功任务可用）",
)
async def download_job(job_id: str) -> FileResponse:
    record = manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")

    # Defensive path check: only files inside this job's published directory
    # can ever be served, and only after the succeeded transition.
    if record.status != "succeeded" or record.download_path is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"download not available for job in status {record.status!r}; "
                "only fully validated succeeded jobs expose a file"
            ),
        )

    work_root = Path(manager.settings.work_dir).resolve()
    job_root = (work_root / job_id).resolve()
    target = record.download_path.resolve()
    try:
        target.relative_to(job_root)
    except ValueError as exc:
        # Never serve anything outside the per-job published directory.
        raise HTTPException(
            status_code=403, detail="invalid download path"
        ) from exc
    if not target.is_file():
        raise HTTPException(status_code=410, detail="file no longer available")

    return FileResponse(
        path=str(target),
        media_type="text/csv",
        filename=target.name,
    )
