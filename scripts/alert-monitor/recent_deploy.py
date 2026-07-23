"""Detect recent co3ntrol-rs process restarts (proxy for code deploys).

Plant hosts do not expose a git SHA over the API. Ops typically `git pull` then
``systemctl restart co3ntrol-rs``, which resets ``app.systick`` (~1 Hz uptime
counter). Crash restarts look the same — Ghost should treat this as
"recent process restart / possible deploy", not a hard proof of a code change.
"""

from __future__ import annotations

from typing import Any


DEFAULT_RECENT_DEPLOY_WINDOW_S = 30 * 60


def extract_systick(sample: dict[str, Any] | None) -> int | None:
    if not sample:
        return None
    app = sample.get("app")
    raw = None
    if isinstance(app, dict):
        raw = app.get("systick")
    if raw is None:
        raw = sample.get("systick")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def host_restart_check(
    *,
    subsystem: str,
    systick: int | None,
    threshold_s: float,
) -> dict[str, Any]:
    if systick is None:
        return {
            "subsystem": subsystem,
            "available": False,
            "recent": False,
            "approx_uptime_s": None,
            "systick": None,
        }
    recent = systick < threshold_s
    return {
        "subsystem": subsystem,
        "available": True,
        "recent": recent,
        "approx_uptime_s": systick,
        "systick": systick,
    }


def build_recent_deploy(
    checks: list[dict[str, Any]],
    *,
    threshold_s: float,
) -> dict[str, Any]:
    available = [c for c in checks if c.get("available")]
    likely = any(c.get("recent") for c in available)
    return {
        "likely": likely,
        "threshold_s": int(threshold_s),
        "checks": checks,
        "signal": "co3ntrol app.systick (process uptime ~1 Hz)",
        "note": (
            "Proxy for a recent code deploy (pull + systemctl restart "
            "co3ntrol-rs). Also true on crash restart — confirm with ops/"
            "Grafana if needed. No git SHA is exposed by the API."
        ),
    }
