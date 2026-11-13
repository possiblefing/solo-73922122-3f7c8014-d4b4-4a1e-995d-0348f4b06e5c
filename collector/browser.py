"""Playwright-driven collection against the legacy web portal.

The portal is operated exactly like a human user would: open the page,
select the station, enter the UTC month, submit the query form and click
the export button.  No portal HTTP API is ever called directly.

Each job gets its own browser process, browser context (isolated cookies,
cache and storage), event listeners and download path, so two jobs never
share a session, window or download.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from .config import Settings


class JobCancelled(Exception):
    """Raised when the client cancelled the job."""


class JobDeadlineExceeded(Exception):
    """Raised when the 60s acceptance-to-completion budget is exhausted."""


class CollectionError(Exception):
    """Raised for portal/browser level collection failures."""


class Deadline:
    """Monotonic deadline with cancellation support."""

    def __init__(self, budget_seconds: float, cancel_event: asyncio.Event):
        self._loop = asyncio.get_running_loop()
        self._deadline = self._loop.time() + budget_seconds
        self._cancel_event = cancel_event

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._loop.time())

    def remaining_ms(self, minimum: float = 250.0) -> int:
        return int(max(minimum, self.remaining()) * 1000)

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self._cancel_event.is_set():
            raise JobCancelled("job cancelled by client")
        if self.remaining() <= 0:
            raise JobDeadlineExceeded("job deadline exceeded")


class BrowserSession:
    """Owns every browser-side resource of a single job."""

    def __init__(self, settings: Settings, cancel_event: asyncio.Event,
                 deadline: Deadline, staging_dir: Path):
        self._settings = settings
        self._cancel_event = cancel_event
        self._deadline = deadline
        self._staging_dir = staging_dir
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._spawned_pages: list[Page] = []
        self._download_event = asyncio.Event()
        self._download: Download | None = None
        self._closed = False

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        # A dedicated browser process per job: full process-level isolation
        # between parallel jobs in addition to context-level isolation.
        self._browser = await self._playwright.chromium.launch(
            headless=self._settings.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
            ],
        )
        self._context = await self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        self._context.set_default_navigation_timeout(
            self._settings.nav_timeout_ms
        )
        self._context.set_default_timeout(self._settings.action_timeout_ms)

        # Context-level listener covers downloads initiated from the main
        # page.  In some Chromium/Playwright builds a download started by a
        # popup window (window.open -> timed redirect -> attachment) is only
        # dispatched on that popup Page object, so every spawned page also
        # gets its own listener in _on_page().  The first event wins.
        self._context.on("download", self._on_download)
        self._context.on("page", self._on_page)

        self._page = await self._context.new_page()

    def _on_download(self, download: Download) -> None:
        # Only the first download of this isolated session is relevant.
        if self._download is None:
            self._download = download
            self._download_event.set()

    def _on_page(self, page: Page) -> None:
        self._spawned_pages.append(page)
        # Popup-initiated downloads may only surface on the popup's Page.
        page.on("download", self._on_download)

    @property
    def page(self) -> Page:
        assert self._page is not None
        return self._page

    async def close(self) -> None:
        """Tear down pages, context, browser process and playwright driver."""
        if self._closed:
            return
        self._closed = True
        for page in list(self._spawned_pages):
            with contextlib.suppress(Exception):
                await page.close()
        self._spawned_pages.clear()
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._context = None
        self._browser = None
        self._playwright = None

    async def collect(self, station: str, month: str) -> Path:
        """Drive the portal and save the CSV into the staging directory."""
        s = self._settings
        portal = s.portal_url
        self._deadline.check()

        # 1. Open the query form.
        try:
            await self.page.goto(
                f"{portal}/",
                wait_until="domcontentloaded",
                timeout=min(s.nav_timeout_ms, self._deadline.remaining_ms()),
            )
        except PlaywrightTimeout as exc:
            raise CollectionError(
                f"portal home did not load: {exc}"
            ) from exc
        self._deadline.check()

        # 2. Choose station and UTC month like a real user.  Verify the
        #    option exists in the portal's own dropdown first: an unknown
        #    station should fail fast with a clear message instead of
        #    waiting out Playwright's action timeout.
        try:
            options = await self.page.eval_on_selector_all(
                "select[name='station'] option",
                "els => els.map(e => e.value)",
            )
        except Exception as exc:  # pragma: no cover - defensive
            raise CollectionError(
                f"portal station dropdown not found: {exc}"
            ) from exc
        if station not in options:
            raise CollectionError(
                f"station {station!r} is not offered by the portal "
                f"(available: {', '.join(options) or 'none'})"
            )
        await self.page.select_option("select[name='station']", station)
        await self.page.fill("input[name='month']", month)

        # 3. Submit the query form and wait for the result table.
        async with self.page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=min(s.action_timeout_ms, self._deadline.remaining_ms()),
        ):
            await self.page.click("button[type='submit']")
        self._deadline.check()

        error_text = await self._page_error_text()
        if error_text:
            raise CollectionError(f"portal rejected query: {error_text}")
        await self.page.wait_for_selector(
            "table.datatable",
            timeout=min(s.action_timeout_ms, self._deadline.remaining_ms()),
        )

        # 4. Click export.  The portal opens a new window which, after its
        #    configured delays, navigates to an attachment URL.
        await self.page.click("button:has-text('导出 CSV')")
        self._deadline.check()

        # 5. Wait for the download, bounded by the remaining job budget so a
        #    late download after cancel/timeout is never awaited forever.
        try:
            await asyncio.wait_for(
                self._wait_for_download(),
                timeout=self._deadline.remaining(),
            )
        except asyncio.TimeoutError as exc:
            self._deadline.check()  # -> JobCancelled / JobDeadlineExceeded
            raise CollectionError(
                "download did not start within the job budget"
            ) from exc

        assert self._download is not None
        download = self._download
        # Confirm the full stream is received; failure() raises on errors.
        try:
            await asyncio.wait_for(
                download.path(), timeout=self._deadline.remaining()
            )
        except asyncio.TimeoutError as exc:
            self._deadline.check()
            raise CollectionError("download did not finish in time") from exc
        with contextlib.suppress(Exception):
            failure = await download.failure()
            if failure:
                raise CollectionError(f"browser reported download failure: "
                                      f"{failure}")

        suggested = download.suggested_filename or "export.csv"
        if not suggested.lower().endswith(".csv"):
            suggested += ".csv"
        target = self._staging_dir / suggested

        # save_as copies the fully received stream into our private staging
        # directory; nothing is published at this point.
        await download.save_as(str(target))
        return target

    async def _wait_for_download(self) -> None:
        wait_task = asyncio.create_task(self._download_event.wait())
        cancel_task = asyncio.create_task(self._cancel_event.wait())
        try:
            await asyncio.wait(
                {wait_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            wait_task.cancel()
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wait_task
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
        if self._cancel_event.is_set():
            raise JobCancelled(
                "job cancelled while waiting for download"
            )
        if self._download is None:
            raise CollectionError(
                "export window closed without a download"
            )

    async def _page_error_text(self) -> str | None:
        try:
            handle = await self.page.query_selector(".err")
        except Exception:
            return None
        if handle is None:
            return None
        text = (await handle.inner_text()).strip()
        return text or None


def build_session(
    settings: Settings,
    staging_dir: Path,
    cancel_event: asyncio.Event,
    deadline: Deadline,
) -> BrowserSession:
    """Factory used by the job manager so it owns (and can close) the
    browser resources during cancellation cleanup."""
    os.makedirs(staging_dir, exist_ok=True)
    return BrowserSession(settings, cancel_event, deadline, staging_dir)
