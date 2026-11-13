"""ASGI-level tests with a fake browser session (no real Chromium needed).

Run:
  PYTHONPATH=/tmp/pylibs python3 -I tests/test_manager.py
"""

from __future__ import annotations

import asyncio
import csv
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from collector import manager as manager_mod  # noqa: E402
from collector.browser import (  # noqa: E402
    CollectionError,
    JobCancelled,
)
from collector.config import Settings  # noqa: E402
from collector.main import app, manager  # noqa: E402

TERMINAL = ("succeeded", "failed", "cancelled", "timeout")


def csv_payload(station: str, month: str, rows: int = 3,
                bad_station: bool = False) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["station", "utc_time", "pm25_ugm3"])
    for i in range(rows):
        w.writerow([
            "ST999" if bad_station else station,
            f"{month}-01T{i:02d}:00:00Z",
            10 + i,
        ])
    return buf.getvalue().encode()


class FakeSession:
    """Simulates BrowserSession: honours cancel events, a collection delay
    and a slow teardown.  close() mirrors the real join-able contract:
    concurrent callers wait for the same single reclamation."""

    def __init__(self, settings, staging_dir, cancel_event, deadline):
        self.staging_dir = Path(staging_dir)
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.scenario_map: dict = {}
        self.closed = False
        self.close_calls = 0
        self._close_lock = asyncio.Lock()
        self._close_delay = 0.0

    async def start(self) -> None:
        return None

    async def collect(self, station: str, month: str) -> Path:
        cfg = self.scenario_map.get(station) or {}
        self._close_delay = cfg.get("close_delay", 0.0)
        delay = cfg.get("delay", 0.0)
        # Cooperative wait that returns immediately on cancel.
        try:
            await asyncio.wait_for(self.cancel_event.wait(),
                                   timeout=delay)
        except asyncio.TimeoutError:
            pass
        if self.cancel_event.is_set():
            raise JobCancelled("cancelled in fake session")
        if cfg.get("fail") is not None:
            raise cfg["fail"]
        payload = cfg.get("payload") or csv_payload(station, month)
        target = self.staging_dir / f"aq_{station}_{month}.csv"
        target.write_bytes(payload)
        return target

    async def close(self) -> None:
        async with self._close_lock:
            if self.closed:
                return
            self.close_calls += 1
            if self._close_delay:
                await asyncio.sleep(self._close_delay)
            self.closed = True


def patch_session(monkeypatch_payloads):
    sessions: list[FakeSession] = []

    def fake_build(settings, staging_dir, cancel_event, deadline):
        # The staging directory name embeds the job id; station-specific
        # behaviour is selected via the module-level scenario map keyed by
        # the submit-time station, which the manager passes to collect().
        session = FakeSession(settings, staging_dir, cancel_event, deadline)
        session.scenario_map = monkeypatch_payloads
        sessions.append(session)
        return session

    manager_mod.build_session = fake_build
    return sessions


async def wait_terminal(client: httpx.AsyncClient, job_id: str,
                        tries: int = 100, interval: float = 0.05) -> dict:
    for _ in range(tries):
        body = (await client.get(f"/collect/jobs/{job_id}")).json()
        if body["status"] in TERMINAL:
            return body
        await asyncio.sleep(interval)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aq-test-")
    # Swap settings used by the app-level manager.
    object.__setattr__(manager, "_settings",
                       Settings(work_dir=tmp, job_timeout_seconds=60,
                                cleanup_timeout_seconds=5))
    sessions = patch_session({
        "STBAD": {"payload": csv_payload("STBAD", "2026-10",
                                         bad_station=True)},
        "STFAIL": {"fail": CollectionError("portal rejected query: boom")},
        "STSLOW": {"delay": 30.0, "payload": csv_payload("STSLOW",
                                                         "2026-10")},
        # Slow teardown: the finalizing phase stays observable for a while.
        "STSLOWCLOSE": {"close_delay": 1.0},
        "STCANCELFIN": {"close_delay": 2.0},
        "STFINLATE": {"close_delay": 2.0},
    })

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        # happy path --------------------------------------------------------
        r = await client.post("/collect/jobs",
                              json={"station": "ST001", "month": "2026-10"})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        body = await wait_terminal(client, job_id)
        assert body["status"] == "succeeded", body
        assert body["rows"] == 3 and body["bytes"] > 0
        assert body["finished_at"] is not None, body
        print("  ok  happy path succeeded:", body["download_url"])
        print("  ok  finished_at filled on success:", body["finished_at"])

        r = await client.get(body["download_url"])
        assert r.status_code == 200 and b"ST001" in r.content
        print(f"  ok  download served ({len(r.content)} bytes)")

        # staging directory must be gone; published dir remains
        work = Path(tmp)
        assert not (work / f"{job_id}.staging").exists()
        assert (work / job_id).is_dir()
        print("  ok  staging reclaimed, published artifact retained")
        assert sessions[-1].closed
        print("  ok  browser session closed after success")

        # validation failure leaves no downloadable residue ----------------
        r = await client.post("/collect/jobs",
                              json={"station": "STBAD", "month": "2026-10"})
        jid = r.json()["job_id"]
        rb = await wait_terminal(client, jid)
        assert rb["status"] == "failed" and "validation" in rb["error"], rb
        assert rb["finished_at"] is not None
        r = await client.get(f"/collect/jobs/{jid}/download")
        assert r.status_code == 409
        assert not (work / jid).exists()
        assert not (work / f"{jid}.staging").exists()
        print("  ok  validation failure -> failed, no residue, download 409")

        # collection error --------------------------------------------------
        r = await client.post("/collect/jobs",
                              json={"station": "STFAIL", "month": "2026-10"})
        jid = r.json()["job_id"]
        rb = await wait_terminal(client, jid)
        assert rb["status"] == "failed" and "boom" in rb["error"], rb
        assert not (work / f"{jid}.staging").exists()
        print("  ok  collection error -> failed with cleanup")

        # cancellation: accept then late download must not publish ----------
        r = await client.post("/collect/jobs",
                              json={"station": "STSLOW", "month": "2026-10"})
        jid = r.json()["job_id"]
        await asyncio.sleep(0.2)
        r = await client.post(f"/collect/jobs/{jid}/cancel")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelling"
        rb = await wait_terminal(client, jid)
        assert rb["status"] == "cancelled", rb
        assert rb["finished_at"] is not None
        r = await client.get(f"/collect/jobs/{jid}/download")
        assert r.status_code == 409
        assert not (work / jid).exists()
        assert not (work / f"{jid}.staging").exists()
        print("  ok  cancellation terminal only after cleanup, no late file")

        r = await client.post(f"/collect/jobs/{jid}/cancel")
        assert r.status_code == 409
        print("  ok  second cancel -> 409")

        # slow teardown: success is reported only after reclamation ---------
        print("\n[slow close] terminal state waits for reclamation")
        r = await client.post(
            "/collect/jobs",
            json={"station": "STSLOWCLOSE", "month": "2026-10"},
        )
        jid = r.json()["job_id"]
        session = sessions[-1]
        saw_finalizing = False
        for _ in range(200):
            rb = (await client.get(f"/collect/jobs/{jid}")).json()
            if rb["status"] == "finalizing":
                saw_finalizing = True
                # Not yet terminal: no finished_at, no download link, and
                # the file is not served while reclamation runs.
                assert rb["finished_at"] is None, rb
                assert rb["download_url"] is None, rb
                r2 = await client.get(f"/collect/jobs/{jid}/download")
                assert r2.status_code == 409, r2.status_code
            if rb["status"] in TERMINAL:
                break
            await asyncio.sleep(0.03)
        assert saw_finalizing, "finalizing phase was never observed"
        assert rb["status"] == "succeeded", rb
        assert rb["finished_at"] is not None, rb
        # Reclamation completed before the terminal state was reported.
        assert session.closed and session.close_calls == 1
        r = await client.get(rb["download_url"])
        assert r.status_code == 200 and b"STSLOWCLOSE" in r.content
        print("  ok  succeeded only after close; download opened at terminal")

        # slow teardown: cancel is still accepted while finalizing ----------
        print("\n[slow close] cancel during finalization")
        r = await client.post(
            "/collect/jobs",
            json={"station": "STCANCELFIN", "month": "2026-10"},
        )
        jid = r.json()["job_id"]
        session = sessions[-1]
        for _ in range(200):
            rb = (await client.get(f"/collect/jobs/{jid}")).json()
            if rb["status"] == "finalizing":
                break
            assert rb["status"] not in TERMINAL, rb
            await asyncio.sleep(0.03)
        else:
            raise AssertionError("job never reached finalizing")
        r = await client.post(f"/collect/jobs/{jid}/cancel")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelling"
        rb = await wait_terminal(client, jid, tries=200)
        # Cancel and success interleaved: exactly one outcome, and the
        # accepted cancellation wins - the file must not be published.
        assert rb["status"] == "cancelled", rb
        assert rb["finished_at"] is not None
        assert rb["download_url"] is None
        r = await client.get(f"/collect/jobs/{jid}/download")
        assert r.status_code == 409
        assert not (work / jid).exists()
        assert not (work / f"{jid}.staging").exists()
        # The cancel-triggered close and the worker's close joined the same
        # single reclamation instead of tearing down twice.
        assert session.closed and session.close_calls == 1, session.close_calls
        print("  ok  cancel during finalizing -> cancelled, no file, "
              "single joined reclamation")

        # deadline expires during finalization ------------------------------
        print("\n[slow close] acceptance deadline arrives while finalizing")
        from collector.main import manager as app_manager

        fast_dir = tempfile.mkdtemp(prefix="aq-fast-")
        original_settings = app_manager._settings
        app_manager._settings = Settings(
            work_dir=fast_dir,
            job_timeout_seconds=1.0,   # budget ends mid-teardown
            cleanup_timeout_seconds=5,
        )
        try:
            r = await client.post(
                "/collect/jobs",
                json={"station": "STFINLATE", "month": "2026-10"},
            )
            jid = r.json()["job_id"]
            session = sessions[-1]
            rb = await wait_terminal(client, jid, tries=200)
            # Reclamation was NOT skipped (close ran to completion) and the
            # would-be success download was NOT retained.
            assert rb["status"] == "timeout", rb
            assert rb["finished_at"] is not None
            assert session.closed, "teardown was skipped on deadline"
            r = await client.get(f"/collect/jobs/{jid}/download")
            assert r.status_code == 409
            assert not (Path(fast_dir) / jid).exists()
            assert not (Path(fast_dir) / f"{jid}.staging").exists()
            print("  ok  deadline during finalizing -> timeout, teardown "
                  "completed, no download retained")
        finally:
            app_manager._settings = original_settings

        # cancel/success interleave: exactly one consistent outcome ---------
        print("\n[race] cancel vs success settles exactly one outcome")
        import random

        rng = random.Random(20261113)
        outcomes = {"succeeded": 0, "cancelled": 0}
        for i in range(12):
            r = await client.post(
                "/collect/jobs",
                json={"station": "ST001", "month": "2026-10"},
            )
            jid = r.json()["job_id"]
            # Cancel at a random moment around the settle boundary.
            await asyncio.sleep(rng.random() * 0.08)
            await client.post(f"/collect/jobs/{jid}/cancel")
            rb = await wait_terminal(client, jid)
            assert rb["status"] in ("succeeded", "cancelled"), rb
            assert rb["finished_at"] is not None
            outcomes[rb["status"]] += 1
            r = await client.get(f"/collect/jobs/{jid}/download")
            if rb["status"] == "cancelled":
                # An accepted cancellation never leaves a published file.
                assert r.status_code == 409
                assert not (work / jid).exists()
            else:
                assert r.status_code == 200
                assert (work / jid).is_dir()
            assert not (work / f"{jid}.staging").exists()
        print(f"  ok  12 interleaved runs settled consistently: {outcomes}")

        # 60s-style budget timeout, compressed to 1 second -------------------
        print("\n[timeout] short acceptance budget must end in 'timeout'")
        original_settings = app_manager._settings
        app_manager._settings = Settings(
            work_dir=tempfile.mkdtemp(prefix="aq-fast-"),
            job_timeout_seconds=1.0,
            cleanup_timeout_seconds=5,
        )
        try:
            r = await client.post(
                "/collect/jobs",
                json={"station": "STSLOW", "month": "2026-10"},
            )
            jid = r.json()["job_id"]
            rb = await wait_terminal(client, jid)
            assert rb["status"] == "timeout", rb
            assert rb["finished_at"] is not None
            r = await client.get(f"/collect/jobs/{jid}/download")
            assert r.status_code == 409
            assert not (Path(app_manager._settings.work_dir)
                        / f"{jid}.staging").exists()
            print("  ok  timeout terminal, download blocked, staging removed")
        finally:
            app_manager._settings = original_settings

        # 404s and 422 -------------------------------------------------------
        assert (await client.get("/collect/jobs/nope")).status_code == 404
        r = await client.post("/collect/jobs",
                              json={"station": "ST001", "month": "2026/10"})
        assert r.status_code == 422
        r = await client.get("/collect/jobs")
        assert r.status_code == 200 and len(r.json()) >= 4
        print("  ok  404/422/listing behave")

        # shutdown must not hang and must reclaim everything ---------------
        await manager.aclose()
        leftovers = [p for p in work.rglob("*.staging")]
        assert not leftovers, leftovers
        print("  ok  shutdown clean")

    print("ALL MANAGER/API TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
