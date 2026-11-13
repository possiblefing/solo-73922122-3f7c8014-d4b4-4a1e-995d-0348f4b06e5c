"""In-memory job manager: lifecycle, isolation, cancellation and cleanup.

Lifecycle:

    pending -> running -> succeeded
        |              |-> failed
        |              |-> cancelling -> cancelled   (cancel accepted first;
        |              |                  terminal only after teardown)
        |              |-> timeout
        -> cancelling -> cancelled                   (cancel while queued)

Guarantees implemented here:
  * every job has a private work directory (staging) and its own browser;
  * a download is only atomically published after full validation;
  * once cancel is accepted, a late download can never be published;
  * the same applies when the deadline fires;
  * terminal status is only reported after browser resources and the
    staging directory have been reclaimed.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .browser import (
    BrowserSession,
    CollectionError,
    Deadline,
    JobCancelled,
    JobDeadlineExceeded,
    build_session,
)
from .config import Settings
from .validation import ValidationError, validate_csv

TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "timeout"}
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class JobRecord:
    job_id: str
    station: str
    month: str
    status: str = "pending"
    created_at: str = field(default_factory=_utcnow_iso)
    started_at: str | None = None
    finished_at: str | None = None
    deadline_at: str = ""
    error: str | None = None
    size_bytes: int | None = None
    rows: int | None = None
    download_path: Path | None = None
    staging_dir: Path | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session: BrowserSession | None = None
    task: asyncio.Task | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES


class JobManager:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._jobs: dict[str, JobRecord] = {}
        self._registry_lock = asyncio.Lock()
        Path(settings.work_dir).mkdir(parents=True, exist_ok=True)

    @property
    def settings(self) -> Settings:
        return self._settings

    async def aclose(self) -> None:
        """Cancel every running job on application shutdown and wait."""
        async with self._registry_lock:
            records = list(self._jobs.values())
        for record in records:
            if not record.is_terminal():
                record.cancel_event.set()
        pending = [
            r.task for r in records if r.task and not r.task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        return sorted(
            self._jobs.values(), key=lambda r: r.created_at, reverse=True
        )

    async def submit(self, station: str, month: str) -> JobRecord:
        job_id = uuid.uuid4().hex
        deadline_iso = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self._settings.job_timeout_seconds)
        ).isoformat(timespec="milliseconds")

        staging_dir = Path(self._settings.work_dir) / f"{job_id}.staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        record = JobRecord(
            job_id=job_id,
            station=station,
            month=month,
            deadline_at=deadline_iso,
            staging_dir=staging_dir,
        )
        async with self._registry_lock:
            self._jobs[job_id] = record
        record.task = asyncio.create_task(
            self._run(record), name=f"aq-job-{job_id}"
        )
        return record

    async def request_cancel(self, job_id: str) -> tuple[bool, str]:
        """Accept cancellation for any non-terminal job.

        Returns (accepted, message).  Once accepted, publishing is blocked
        even if the download arrives moments later.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return False, "job not found"
        async with record.state_lock:
            if record.is_terminal():
                return False, f"job already terminal: {record.status}"
            # Non-terminal visible state until teardown completes; the worker
            # flips this to the terminal "cancelled" only after cleanup.
            record.status = "cancelling"
            record.error = "cancellation accepted; tearing down browser work"
        # Signal first, then proactively tear the browser down so waiting on
        # a slow popup/download is interrupted immediately.
        record.cancel_event.set()
        if record.session is not None:
            asyncio.create_task(record.session.close())
        return True, "cancellation accepted"

    # ------------------------------------------------------------------ #
    # Internal worker
    # ------------------------------------------------------------------ #

    async def _run(self, record: JobRecord) -> None:
        budget = self._settings.job_timeout_seconds
        deadline = Deadline(budget, record.cancel_event)
        try:
            async with asyncio.timeout(budget):
                await self._work(record, deadline)
        except (asyncio.CancelledError, JobCancelled):
            await self._finish(record, "cancelled",
                               "cancelled by client before completion")
        except TimeoutError:  # asyncio.timeout() budget exhausted
            record.cancel_event.set()
            await self._finish(
                record, "timeout",
                f"job exceeded {budget:.0f}s acceptance budget",
            )
        except JobDeadlineExceeded as exc:
            record.cancel_event.set()
            await self._finish(record, "timeout", str(exc))
        except CollectionError as exc:
            await self._finish(record, "failed", str(exc))
        except ValidationError as exc:
            await self._finish(
                record, "failed", f"csv validation failed: {exc}"
            )
        except Exception as exc:  # defensive: never leave a job dangling
            await self._finish(
                record, "failed",
                f"unexpected error: {type(exc).__name__}: {exc}",
            )

    async def _work(self, record: JobRecord, deadline: Deadline) -> None:
        async with record.state_lock:
            if record.status == "cancelling" or record.is_terminal():
                # Cancel landed while the task was still queued.
                raise JobCancelled(record.error or "cancelled before start")
            record.status = "running"
            record.started_at = _utcnow_iso()
        assert record.staging_dir is not None
        session = build_session(
            self._settings,
            record.staging_dir,
            record.cancel_event,
            deadline,
        )
        record.session = session
        try:
            await session.start()
            staging_path = await session.collect(
                record.station, record.month
            )

            # Publish gate: a cancel accepted at any point before validation
            # completes blocks publication; the late download is discarded.
            if record.cancel_event.is_set():
                raise JobCancelled(
                    "cancellation accepted; download not published"
                )
            deadline.check()

            payload = await asyncio.to_thread(staging_path.read_bytes)
            result = await asyncio.to_thread(
                validate_csv, payload, record.station, record.month
            )

            if record.cancel_event.is_set():
                raise JobCancelled(
                    "cancellation accepted; download not published"
                )
            deadline.check()

            published_dir = (
                Path(self._settings.work_dir) / record.job_id
            )
            published_dir.mkdir(parents=True, exist_ok=True)
            published_path = published_dir / staging_path.name
            # Atomic move on the same volume: readers never see a partial file.
            await asyncio.to_thread(
                shutil.move, str(staging_path), str(published_path)
            )

            async with record.state_lock:
                if record.cancel_event.is_set() or deadline.expired():
                    # Cancel/deadline landed in the tiny window around the
                    # move: remove the artifact so it can never be served.
                    await asyncio.to_thread(
                        shutil.rmtree, published_dir, True
                    )
                    if record.cancel_event.is_set():
                        raise JobCancelled(
                            "cancelled after publication attempt"
                        )
                    raise JobDeadlineExceeded(
                        "deadline exceeded during publish"
                    )
                record.download_path = published_path
                record.size_bytes = result.size_bytes
                record.rows = result.rows
                record.status = "succeeded"
        finally:
            # Browser teardown happens for every outcome, before terminal
            # state is reported.
            await session.close()
            record.session = None

        # Success path: reclaim the (now empty) staging directory.
        await self._cleanup_staging(record)

    async def _finish(self, record: JobRecord, status: str,
                      message: str) -> None:
        """Terminal transition performed only after full teardown."""
        if record.session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    record.session.close(),
                    timeout=self._settings.cleanup_timeout_seconds,
                )
            record.session = None
        await self._cleanup_staging(record)
        async with record.state_lock:
            if not record.is_terminal():
                # "cancelling" settles to the terminal "cancelled" here,
                # only after every browser resource and staging artifact
                # has been reclaimed.
                if record.status == "cancelling":
                    record.status = "cancelled"
                    record.error = (
                        "cancelled by client; browser work and downloads "
                        "reclaimed"
                    )
                else:
                    record.status = status
                    record.error = message
            record.finished_at = _utcnow_iso()

    async def _cleanup_staging(self, record: JobRecord) -> None:
        if record.staging_dir is None:
            return

        def _remove() -> None:
            shutil.rmtree(record.staging_dir, ignore_errors=True)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(_remove),
                timeout=self._settings.cleanup_timeout_seconds,
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                shutil.rmtree(record.staging_dir, ignore_errors=True)
