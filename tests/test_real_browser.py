"""Drive the real Python BrowserSession against the local mock portal.

Uses the project's own collector.browser module with a real Chromium,
proving the production Playwright code path works (not just the Node port).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.browser import BrowserSession, Deadline  # noqa: E402
from collector.config import Settings  # noqa: E402
from collector.validation import validate_csv  # noqa: E402


async def test_join_close() -> None:
    """A repeated close() must wait for the same in-progress reclamation
    instead of returning early or tearing resources down twice."""
    settings = Settings(close_delay_seconds=1.0)
    cancel = asyncio.Event()
    deadline = Deadline(60.0, cancel)
    session = BrowserSession(
        settings, cancel, deadline, Path(tempfile.mkdtemp(prefix="aq-join-"))
    )
    t0 = time.monotonic()
    first = asyncio.create_task(session.close())
    await asyncio.sleep(0.3)  # first close is now mid-teardown
    second = asyncio.create_task(session.close())
    await second
    joined_after = time.monotonic() - t0
    await first
    total = time.monotonic() - t0
    # The second call joined the same reclamation: it returned only when
    # the first teardown finished (~1.0s) - not immediately (~0.3s) - and
    # it did not run a second teardown (~1.3s+).
    assert 0.55 < joined_after < 1.2, joined_after
    assert total < 1.6, total
    # A close after completion returns immediately.
    t1 = time.monotonic()
    await session.close()
    assert time.monotonic() - t1 < 0.2
    print("  ok  repeated close joins the same in-progress reclamation")


async def main() -> int:
    await test_join_close()

    work = Path(tempfile.mkdtemp(prefix="aq-py-pw-"))
    staging = work / "staging"
    staging.mkdir()
    settings = Settings(
        portal_url="http://127.0.0.1:8091",
        headless=True,
        nav_timeout_ms=20000,
        action_timeout_ms=15000,
    )
    cancel = asyncio.Event()
    deadline = Deadline(60.0, cancel)
    session = BrowserSession(settings, cancel, deadline, staging)
    try:
        await session.start()
        csv_path = await session.collect("ST002", "2026-10")
        payload = csv_path.read_bytes()
        result = validate_csv(payload, "ST002", "2026-10")
        print(f"  ok  python playwright collected {csv_path.name}: "
              f"{result.rows} rows, {result.size_bytes} bytes")
        assert payload.count(b"ST002,2026-10") == result.rows
    finally:
        await session.close()
        print("  ok  python browser session closed cleanly")

    # Cancellation path with a deliberately delayed portal (popup 2s +
    # download 3s). Cancel after 0.8s, well before the file is produced.
    slow_settings = Settings(
        portal_url="http://127.0.0.1:8092",
        headless=True,
        nav_timeout_ms=20000,
        action_timeout_ms=15000,
    )
    staging2 = work / "staging2"
    staging2.mkdir()
    cancel2 = asyncio.Event()
    deadline2 = Deadline(60.0, cancel2)
    session2 = BrowserSession(slow_settings, cancel2, deadline2, staging2)
    from collector.browser import JobCancelled

    async def cancel_soon() -> None:
        await asyncio.sleep(0.8)
        cancel2.set()
        await session2.close()

    try:
        await session2.start()
        canceller = asyncio.create_task(cancel_soon())
        try:
            await session2.collect("ST004", "2026-10")
        except JobCancelled:
            print("  ok  python session raised JobCancelled after cancel")
        else:
            raise AssertionError("expected JobCancelled")
        await canceller
    finally:
        await session2.close()
        print("  ok  cancelled python session reclaimed")

    print("PYTHON PLAYWRIGHT REAL-BROWSER TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
