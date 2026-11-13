"""In-memory job manager: lifecycle, isolation, cancellation and cleanup.

Lifecycle:

    pending -> running -> finalizing -> succeeded
        |         |           |     |-> failed
        |         |           |     |-> cancelled
        |         |           |     |-> timeout
        |         |           |-> cancelling -> (finalizing) -> cancelled
        |         |-> cancelling
        |-> cancelling

    "finalizing" is the teardown phase: collection has finished and the
    browser session (popup windows, in-flight downloads, context, browser
    process, driver) plus the staging directory are being reclaimed.  A
    terminal state and finished_at are only published after that
    reclamation completes; a successful file becomes downloadable only at
    that moment.  Cancellation is still accepted while finalizing.

Guarantees implemented here:
  * every job has a private work directory (staging) and its own browser;
  * a download is only atomically published after full validation, and only
    becomes visible together with the terminal "succeeded" state;
  * once cancel is accepted, a late download can never be published;
  * the same applies when the acceptance deadline fires - even if it fires
    while finalization is already in progress (reclamation is never
    skipped and a would-be success download is never retained);
  * cancellation and success may interleave, but exactly one outcome is
    settled, decided atomically under the per-job state lock;
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
from .validation import ValidationError, ValidationResult, validate_csv

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
    # Validated artifact still sitting in the private staging directory,
    # waiting for the finalization phase to publish or discard it.
    staging_file: Path | None = None
    validation: ValidationResult | None = None

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
            async with record.state_lock:
                if record.is_terminal():
                    continue
                # Shutdown is a cancellation: the settle step will report
                # the terminal state only after teardown completes.
                record.status = "cancelling"
                record.cancel_event.set()
            if record.session is not None:
                # Proactively interrupt slow browser waits; the worker
                # joins this same reclamation via the session close lock.
                asyncio.create_task(record.session.close())
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

        Returns (accepted, message).  Cancellation is still accepted while
        the job is finalizing; once accepted, publishing is blocked even
        if the download arrives moments later.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return False, "job not found"
        async with record.state_lock:
            if record.is_terminal():
                return False, f"job already terminal: {record.status}"
            # Non-terminal visible state until teardown completes; the
            # settle step flips this to the terminal "cancelled" only
            # after cleanup.
            record.status = "cancelling"
            record.error = "cancellation accepted; tearing down browser work"
        # Signal first, then proactively tear the browser down so waiting
        # on a slow popup/download is interrupted immediately.  The worker
        # joins this very same reclamation via the session's close lock -
        # it is never executed twice.
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
        proposed: str
        message: str | None
        try:
            # Phase 1 (collection) is bounded by the acceptance budget.
            # Phase 2 (finalization/teardown) deliberately runs outside
            # this timeout: a deadline expiring during teardown must never
            # skip the reclamation.
            async with asyncio.timeout(budget):
                proposed, message = await self._collect(record, deadline)
        except (asyncio.CancelledError, JobCancelled) as exc:
            proposed = "cancelled"
            message = str(exc) or "cancelled by client before completion"
        except TimeoutError:  # asyncio.timeout() budget exhausted
            record.cancel_event.set()
            proposed = "timeout"
            message = f"job exceeded {budget:.0f}s acceptance budget"
        except JobDeadlineExceeded as exc:
            record.cancel_event.set()
            proposed = "timeout"
            message = str(exc)
        except CollectionError as exc:
            proposed = "failed"
            message = str(exc)
        except ValidationError as exc:
            proposed = "failed"
            message = f"csv validation failed: {exc}"
        except Exception as exc:  # defensive: never leave a job dangling
            proposed = "failed"
            message = f"unexpected error: {type(exc).__name__}: {exc}"

        try:
            await self._finalize(record, proposed, message, deadline)
        except asyncio.CancelledError:
            async with record.state_lock:
                if not record.is_terminal():
                    record.status = "cancelled"
                    record.error = "worker cancelled during finalization"
                    record.finished_at = _utcnow_iso()
            raise
        except Exception as exc:  # last resort: never stay non-terminal
            async with record.state_lock:
                if not record.is_terminal():
                    record.status = "failed"
                    record.error = (
                        f"finalization error: {type(exc).__name__}: {exc}"
                    )
                    record.finished_at = _utcnow_iso()

    async def _collect(
        self, record: JobRecord, deadline: Deadline
    ) -> tuple[str, str | None]:
        """Drive the browser and validate the download.

        Returns the proposed outcome.  Nothing is published here and no
        terminal state is reported: the validated file stays in the
        private staging directory until finalization completes.
        """
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

        await session.start()
        staging_path = await session.collect(record.station, record.month)

        # Publish gate: a cancel accepted at any point before validation
        # completes blocks publication; the late download is discarded.
        deadline.check()

        payload = await asyncio.to_thread(staging_path.read_bytes)
        result = await asyncio.to_thread(
            validate_csv, payload, record.station, record.month
        )

        deadline.check()

        record.staging_file = staging_path
        record.validation = result
        return "succeeded", None

    async def _finalize(
        self,
        record: JobRecord,
        proposed: str,
        message: str | None,
        deadline: Deadline,
    ) -> None:
        """Reclaim every resource, then settle exactly one terminal state.

        Runs outside the acceptance budget: even if the 60s deadline
        arrives while this phase is in progress, reclamation is carried
        out to completion and a would-be success download is discarded.
        """
        async with record.state_lock:
            if record.status != "cancelling":
                record.status = "finalizing"

        # 1. Reclaim the browser session: in-flight download, popup
        #    windows, main page, context, browser process and driver.  A
        #    close already triggered by request_cancel is joined via the
        #    session's close lock, never duplicated.
        if record.session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    record.session.close(),
                    timeout=self._settings.cleanup_timeout_seconds,
                )
            record.session = None

        # 2. Publish the validated artifact - unless a cancel/deadline
        #    already landed, in which case the staging file is discarded
        #    with the staging directory below.
        published_path: Path | None = None
        if (
            proposed == "succeeded"
            and record.staging_file is not None
            and not record.cancel_event.is_set()
            and not deadline.expired()
        ):
            published_dir = Path(self._settings.work_dir) / record.job_id
            published_dir.mkdir(parents=True, exist_ok=True)
            published_path = published_dir / record.staging_file.name
            # Atomic move on the same volume: readers never see a partial
            # file.  Still not servable: status is not terminal yet.
            await asyncio.to_thread(
                shutil.move, str(record.staging_file), str(published_path)
            )

        # 3. Reclaim the private staging directory (now empty, or still
        #    holding a download that must never be published).
        await self._cleanup_staging(record)

        # 4. Settle exactly one outcome, atomically with request_cancel.
        async with record.state_lock:
            if record.status == "cancelling":
                # A cancellation accepted at any point before this moment
                # wins over success/failure/timeout alike.
                final = "cancelled"
                final_message = (
                    "cancelled by client; browser work and downloads "
                    "reclaimed"
                )
            elif proposed == "succeeded" and deadline.expired():
                # The acceptance budget ran out during finalization: the
                # success download must not be retained.
                final = "timeout"
                final_message = (
                    "acceptance budget reached during finalization; "
                    "download discarded"
                )
            else:
                final = proposed
                final_message = message

            if final == "succeeded" and published_path is not None:
                assert record.validation is not None
                record.download_path = published_path
                record.size_bytes = record.validation.size_bytes
                record.rows = record.validation.rows
            else:
                # Cancel/deadline interleaved with the publish move, or
                # any non-success outcome: nothing may remain downloadable.
                record.download_path = None
                if published_path is not None:
                    await asyncio.to_thread(
                        shutil.rmtree, published_path.parent, True
                    )
            record.status = final
            record.error = final_message
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
