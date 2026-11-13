"""Runtime configuration for the collection API."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # The portal address is fixed configuration, never supplied per request.
    portal_url: str = os.environ.get(
        "PORTAL_URL", "http://portal:8080"
    ).rstrip("/")
    job_timeout_seconds: float = float(
        os.environ.get("JOB_TIMEOUT_SECONDS", "60")
    )
    # Hard stop for the post-deadline teardown phase.
    cleanup_timeout_seconds: float = float(
        os.environ.get("CLEANUP_TIMEOUT_SECONDS", "15")
    )
    work_dir: str = os.environ.get("WORK_DIR", "/tmp/aq-jobs")
    headless: bool = os.environ.get("HEADLESS", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    # Bounded waits used while driving the real browser; always below the
    # overall job deadline so cancellation/timeout stay responsive.
    nav_timeout_ms: int = int(float(_env_float("NAV_TIMEOUT_SECONDS", 20)) * 1000)
    action_timeout_ms: int = int(
        float(_env_float("ACTION_TIMEOUT_SECONDS", 15)) * 1000
    )


settings = Settings()
