"""Runtime configuration for the headless-scraping-subsystem.

Settings are loaded from environment variables (with sensible defaults for
local development). When deployed on Debian via the bundled systemd unit, the
service reads its environment from ``/etc/headless-scraping-subsystem.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- Network --------------------------------------------------------
    bind_host: str = os.environ.get("BIND_HOST", "127.0.0.1")
    bind_port: int = _env_int("BIND_PORT", 8088)

    # --- Auth -----------------------------------------------------------
    # If empty, auth is disabled. Production deployments MUST set this.
    bearer_token: str = os.environ.get("BEARER_TOKEN", "")

    # --- Browser pool ---------------------------------------------------
    browser_pool_size: int = _env_int("BROWSER_POOL_SIZE", 2)
    # Per-challenge ceiling: a single solve must not exceed this many seconds
    challenge_timeout_s: float = _env_float("CHALLENGE_TIMEOUT_S", 60.0)
    # Per-feed wait: the browser pauses on each outbound fetch and waits for
    # Scrapy Cloud to feed the response via /feed. This is the upper bound.
    feed_wait_timeout_s: float = _env_float("FEED_WAIT_TIMEOUT_S", 45.0)
    # How long a finished challenge result is retained for pickup.
    result_retention_s: float = _env_float("RESULT_RETENTION_S", 120.0)

    # --- Playwright -----------------------------------------------------
    headless: bool = _env_bool("HEADLESS", True)
    # Path overrides — usually unset; Playwright resolves its own browsers.
    playwright_browsers_path: str = os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH", ""
    )

    # --- Logging --------------------------------------------------------
    log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()

    # --- Operational ----------------------------------------------------
    # Maximum number of outbound fetches a single challenge solve may issue.
    max_fetches_per_challenge: int = _env_int("MAX_FETCHES_PER_CHALLENGE", 50)


settings = Settings()
