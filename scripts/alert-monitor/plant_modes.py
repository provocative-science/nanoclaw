"""Operator-set plant modes shared by the alert monitor and NanoClaw.

File: $NANOCLAW_ROOT/data/alert-monitor/plant-modes.json. NanoClaw writes it (chat
commands like "mute container alerts for 12h", Ghost's set_plant_mode tool); the
monitor reads it on every alert and enforces it before any agent run.

    {
      "subsystems": {
        "container": {"mode": "offline" | "maintenance" | "muted", "until": ISO-UTC | null,
                      "reason": str, "set_by": str, "set_at": ISO-UTC,
                      "chat_jid": str | null, "notify": "none" | "line"}
      },
      "muted_bits": [{"subsystem": "liquefaction", "severity": "warning" | "error", "bit": 2,
                      "until": ISO-UTC | null, "reason": str, "set_by": str, "set_at": ISO-UTC}]
    }

offline = not operating until an operator says otherwise; maintenance = planned work,
ends at `until`; muted = alerts off without a stated plant state. While a mode is
active the monitor records events but starts no agent run; notify "line" posts one
plain line per new incident instead of a brief.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODES_REL_PATH = Path("data") / "alert-monitor" / "plant-modes.json"
MODES = ("muted", "maintenance", "offline")


def modes_path(nanoclaw_root: Path) -> Path:
    return nanoclaw_root / MODES_REL_PATH


def _ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(nanoclaw_root: Path) -> dict[str, Any]:
    try:
        data = json.loads(modes_path(nanoclaw_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"subsystems": {}, "muted_bits": []}
    if not isinstance(data, dict):
        return {"subsystems": {}, "muted_bits": []}
    subs = data.get("subsystems") if isinstance(data.get("subsystems"), dict) else {}
    bits = data.get("muted_bits") if isinstance(data.get("muted_bits"), list) else []
    return {"subsystems": subs, "muted_bits": [b for b in bits if isinstance(b, dict)]}


def save(nanoclaw_root: Path, data: dict[str, Any]) -> None:
    path = modes_path(nanoclaw_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _live(entry: dict[str, Any], now: float) -> bool:
    until = _ts(entry.get("until"))
    return until is None or until > now


def active_mode(nanoclaw_root: Path, subsystem: str, now: float) -> dict[str, Any] | None:
    entry = load(nanoclaw_root)["subsystems"].get(subsystem)
    if isinstance(entry, dict) and entry.get("mode") in MODES and _live(entry, now):
        return {"subsystem": subsystem, **entry}
    return None


def muted_bits(nanoclaw_root: Path, subsystem: str, severity: str, now: float) -> set[int]:
    return {
        int(b["bit"])
        for b in load(nanoclaw_root)["muted_bits"]
        if b.get("subsystem") == subsystem
        and b.get("severity") == severity
        and isinstance(b.get("bit"), int)
        and _live(b, now)
    }


def expire(nanoclaw_root: Path, now: float) -> list[dict[str, Any]]:
    """Drop entries whose `until` has passed; returns what was removed."""
    data = load(nanoclaw_root)
    removed: list[dict[str, Any]] = []
    for name, entry in list(data["subsystems"].items()):
        if isinstance(entry, dict) and not _live(entry, now):
            removed.append({"subsystem": name, **entry})
            del data["subsystems"][name]
    keep = []
    for b in data["muted_bits"]:
        (keep if _live(b, now) else removed).append(b)
    data["muted_bits"] = keep
    if removed:
        save(nanoclaw_root, data)
    return removed
