"""Shared plant-alert log for all NanoClaw sessions.

Host path: $NANOCLAW_ROOT/groups/global/shared/plant-alerts/
Container path (every group): /workspace/shared/plant-alerts/

Written by the host monitor on fire; Ghost appends the operator brief after
analysis so follow-ups in any chat (including DMs) can read the same context.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_LOG_LINES = 50

PLANT_ALERTS_REL = Path("groups") / "global" / "shared" / "plant-alerts"


def plant_alerts_dir(nanoclaw_root: Path) -> Path:
    return nanoclaw_root / PLANT_ALERTS_REL


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _trim_jsonl(path: Path, max_lines: int = MAX_LOG_LINES) -> None:
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= max_lines:
        return
    _atomic_write(path, "\n".join(lines[-max_lines:]) + "\n")


def _compact_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Keep log entries readable — drop bulky sensor dumps if present."""
    keep = {
        "subsystem",
        "kind",
        "fired_at",
        "tower",
        "conditions",
        "new_conditions",
        "phase",
        "recent_deploy",
    }
    out = {k: alert[k] for k in keep if k in alert}
    # Small extras useful for follow-ups
    sensors = alert.get("sensors")
    if isinstance(sensors, dict):
        slim = {
            k: sensors[k]
            for k in (
                "system_state",
                "system_state_code",
                "active_warnings",
                "active_errors",
                "accumulator_pressure_psi",
            )
            if k in sensors
        }
        if slim:
            out["sensors"] = slim
    return out


def _format_latest_md(entry: dict[str, Any]) -> str:
    alert = entry.get("alert") or {}
    brief = entry.get("brief")
    lines = [
        "# Latest plant alert",
        "",
        f"- **id:** `{entry.get('id')}`",
        f"- **fired_at:** {entry.get('fired_at') or alert.get('fired_at')}",
        f"- **subsystem:** {alert.get('subsystem')}",
        f"- **kind:** {alert.get('kind')}",
        "",
    ]
    if brief:
        lines.extend(["## Operator brief", "", str(brief).strip(), ""])
    else:
        lines.extend(
            [
                "## Status",
                "",
                "Host monitor recorded the edge; Ghost brief not written yet "
                "(or still running).",
                "",
                "## Event snapshot",
                "",
                "```json",
                json.dumps(alert, indent=2, default=str),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Follow-ups",
            "",
            "Any NanoClaw session can read this file and `log.jsonl` under "
            "`/workspace/shared/plant-alerts/`. Prefer `latest.md` for the "
            "most recent alert; scan `log.jsonl` for history.",
            "",
        ]
    )
    return "\n".join(lines)


def record_alert_event(
    nanoclaw_root: Path,
    *,
    alert_id: str,
    alert: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append a host-side alert event and refresh latest.md (pre-brief)."""
    entry = {
        "id": alert_id,
        "recorded_at": _utc_now_iso(),
        "fired_at": alert.get("fired_at"),
        "source": "alert-monitor",
        "alert": _compact_alert(alert),
        "brief": None,
    }
    directory = plant_alerts_dir(nanoclaw_root)
    log_path = directory / "log.jsonl"
    latest_path = directory / "latest.md"
    event_path = directory / "latest-event.json"

    if dry_run:
        return {
            "dry_run": True,
            "entry": entry,
            "log_path": str(log_path),
            "latest_path": str(latest_path),
        }

    directory.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    _trim_jsonl(log_path)
    _atomic_write(event_path, json.dumps(entry, indent=2, default=str) + "\n")
    _atomic_write(latest_path, _format_latest_md(entry))
    return {
        "dry_run": False,
        "entry": entry,
        "log_path": str(log_path),
        "latest_path": str(latest_path),
    }
