#!/usr/bin/env python3
"""End-to-end demo against the running docker-compose stack.

Stdlib only.  Exercises:
  1. parallel collection of two stations (isolated browsers/jobs)
  2. status polling and validated CSV download
  3. bad-data station (STBAD) -> failed, no downloadable residue
  4. unknown station -> failed
  5. cancellation of a running job (needs the slow portal profile)
  6. 60s timeout (only when SLOW=1 with the slow portal profile)

Usage:
  python -I tests/e2e_demo.py [base_url]
  SLOW=1 python -I tests/e2e_demo.py            # adds cancel + timeout cases
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SLOW = os.environ.get("SLOW") == "1"
DEADLINE = 90 if not SLOW else 180


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | bytes]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw)
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def submit(station: str, month: str) -> str:
    status, body = call("POST", "/collect/jobs",
                        {"station": station, "month": month})
    assert status == 202, (status, body)
    print(f"  submitted {station} {month} -> {body['job_id']} "
          f"({body['status']})")
    return body["job_id"]


def wait_terminal(job_id: str, timeout: float = DEADLINE) -> dict:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        status, body = call("GET", f"/collect/jobs/{job_id}")
        assert status == 200, (status, body)
        if body["status"] in ("succeeded", "failed", "cancelled", "timeout"):
            return body
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} did not finish within {timeout}s")


def check_download_blocked(job_id: str, expect_status: int) -> None:
    status, body = call("GET", f"/collect/jobs/{job_id}/download")
    assert status == expect_status, (status, body)
    print(f"  ok  download on {job_id} blocked as expected "
          f"(HTTP {status})")


def main() -> int:
    print(f"== target: {BASE} (slow profile: {SLOW}) ==")

    # 1-2. two parallel jobs ------------------------------------------------
    print("\n[1] parallel collection of two stations")
    j1 = submit("ST001", "2026-10")
    j2 = submit("ST002", "2026-10")
    r1 = wait_terminal(j1)
    r2 = wait_terminal(j2)
    assert r1["status"] == "succeeded", r1
    assert r2["status"] == "succeeded", r2
    print(f"  ok  both succeeded: rows={r1['rows']}/{r2['rows']} "
          f"bytes={r1['bytes']}/{r2['bytes']}")
    assert r1["download_url"] and r2["download_url"]

    status, csv1 = call("GET", r1["download_url"])
    assert status == 200
    assert b"ST001" in csv1 and b"2026-10" in csv1
    print(f"  ok  downloaded {len(csv1)} bytes, header: "
          f"{csv1.splitlines()[0].decode()}")
    status, csv2 = call("GET", r2["download_url"])
    assert status == 200 and b"ST002" in csv2
    print("  ok  second isolated job downloaded independently")

    # listing works
    status, jobs = call("GET", "/collect/jobs")
    assert status == 200 and len(jobs) >= 2
    print(f"  ok  job list contains {len(jobs)} entries")

    # 3. bad CSV station column -> validation failure, no residue -----------
    print("\n[2] malformed portal output (STBAD) must be rejected")
    jb = submit("STBAD", "2026-10")
    rb = wait_terminal(jb)
    assert rb["status"] == "failed", rb
    assert "validation" in (rb["error"] or "")
    print(f"  ok  STBAD failed validation: {rb['error']}")
    check_download_blocked(jb, 409)

    # 4. unknown station -----------------------------------------------------
    print("\n[3] unknown station must fail cleanly")
    ju = submit("STNOPE", "2026-10")
    ru = wait_terminal(ju)
    assert ru["status"] == "failed", ru
    print(f"  ok  unknown station failed: {ru['error']}")
    check_download_blocked(ju, 409)

    # 404 handling
    status, _ = call("GET", "/collect/jobs/does-not-exist")
    assert status == 404
    print("  ok  unknown job id -> 404")

    # request validation
    status, body = call("POST", "/collect/jobs",
                        {"station": "ST001", "month": "10/2026"})
    assert status == 422, (status, body)
    print("  ok  malformed month -> 422")

    if SLOW:
        # 5. cancellation ----------------------------------------------------
        print("\n[4] cancellation while portal window/download is delayed")
        jc = submit("ST003", "2026-10")
        time.sleep(1.0)
        status, body = call("POST", f"/collect/jobs/{jc}/cancel")
        assert status == 200, (status, body)
        print(f"  ok  cancel accepted: {body['message']}")
        rc = wait_terminal(jc, timeout=45)
        assert rc["status"] == "cancelled", rc
        print(f"  ok  terminal state: {rc['status']} ({rc['error']})")
        check_download_blocked(jc, 409)

        # cancelling twice is a 409, not a crash
        status, body = call("POST", f"/collect/jobs/{jc}/cancel")
        assert status == 409, (status, body)
        print("  ok  second cancel -> 409")

        # 6. timeout ---------------------------------------------------------
        print("\n[5] 60s acceptance timeout (portal takes ~100s)")
        jt = submit("ST004", "2026-10")
        rt = wait_terminal(jt, timeout=90)
        assert rt["status"] == "timeout", rt
        print(f"  ok  terminal state: {rt['status']} ({rt['error']})")
        check_download_blocked(jt, 409)
    else:
        print("\n[skip] cancel/timeout cases: rerun with SLOW=1 against the "
              "docker-compose.slow.yml profile")

    print("\nALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
