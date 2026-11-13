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
    """Simulates BrowserSession: honours cancel events and a delay."""

    def __init__(self, settings, staging_dir, cancel_event, deadline,
                 payload: bytes | None = None, delay: float = 0.0,
                 fail: Exception | None = None):
        self.staging_dir = Path(staging_dir)
        self.cancel_event = cancel_event
        self.deadline = deadline
        self._payload = payload
        self._delay = delay
        self._fail = fail
        self.closed = False
        self.scenario_map: dict = {}

    async def start(self) -> None:
        return None

    async def collect(self, station: str, month: str) -> Path:
        cfg = self.scenario_map.get(station)
        delay = cfg.get("delay", 0.0) if cfg else 0.0
        # Cooperative wait that returns immediately on cancel.
        try:
            await asyncio.wait_for(self.cancel_event.wait(),
                                   timeout=delay)
        except asyncio.TimeoutError:
            pass
        if self.cancel_event.is_set():
            raise JobCancelled("cancelled in fake session")
        if cfg and cfg.get("fail") is not None:
            raise cfg["fail"]
        payload = cfg["payload"] if cfg else csv_payload(station, month)
        target = self.staging_dir / f"aq_{station}_{month}.csv"
        target.write_bytes(payload)
        return target
    async def close(self) -> None:
        self.closed = True


def patch_session(monkeypatch_payloads):
    sessions: list[FakeSession] = []

    def fake_build(settings, staging_dir, cancel_event, deadline):
        # The staging directory name embeds the job id; station-specific
        # behaviour is selected via the module-level scenario map keyed by
        # the submit-time station, which the manager passes to collect().
        session = FakeSession(
            settings, staging_dir, cancel_event, deadline,
            payload=None, delay=0.0, fail=None,
        )
        session.scenario_map = monkeypatch_payloads
        sessions.append(session)
        return session

    manager_mod.build_session = fake_build
    return sessions


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
    })

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        # happy path --------------------------------------------------------
        r = await client.post("/collect/jobs",
                              json={"station": "ST001", "month": "2026-10"})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        for _ in range(50):
            r = await client.get(f"/collect/jobs/{job_id}")
            if r.json()["status"] in (
                "succeeded", "failed", "cancelled", "timeout"
            ):
                break
            await asyncio.sleep(0.05)
        body = r.json()
        assert body["status"] == "succeeded", body
        assert body["rows"] == 3 and body["bytes"] > 0
        print("  ok  happy path succeeded:", body["download_url"])

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
        for _ in range(50):
            rb = (await client.get(f"/collect/jobs/{jid}")).json()
            if rb["status"] in ("succeeded", "failed", "cancelled",
                                "timeout"):
                break
            await asyncio.sleep(0.05)
        assert rb["status"] == "failed" and "validation" in rb["error"], rb
        r = await client.get(f"/collect/jobs/{jid}/download")
        assert r.status_code == 409
        assert not (work / jid).exists()
        assert not (work / f"{jid}.staging").exists()
        print("  ok  validation failure -> failed, no residue, download 409")

        # collection error --------------------------------------------------
        r = await client.post("/collect/jobs",
                              json={"station": "STFAIL", "month": "2026-10"})
        jid = r.json()["job_id"]
        for _ in range(50):
            rb = (await client.get(f"/collect/jobs/{jid}")).json()
            if rb["status"] != "running" and rb["status"] != "pending":
                break
            await asyncio.sleep(0.05)
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
        for _ in range(80):
            rb = (await client.get(f"/collect/jobs/{jid}")).json()
            if rb["status"] in ("succeeded", "failed", "cancelled",
                                "timeout"):
                break
            await asyncio.sleep(0.05)
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

        # 60s-style budget timeout, compressed to 1 second -------------------
        print("\n[timeout] short acceptance budget must end in 'timeout'")
        from collector.main import manager as app_manager

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
            for _ in range(80):
                rb = (await client.get(f"/collect/jobs/{jid}")).json()
                if rb["status"] in ("succeeded", "failed", "cancelled",
                                    "timeout"):
                    break
                await asyncio.sleep(0.05)
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
