"""Shared mute flag for Ghost plant alerts (host monitor + NanoClaw).

State file: $NANOCLAW_ROOT/data/alert-monitor/mute.json

Written when a user tags Ghost with "mute alerts" / "unmute alerts".
The alert monitor skips IPC fires while muted; plant edge state still advances.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

MuteCommand = Literal["mute", "unmute"]

MUTE_REL_PATH = Path("data") / "alert-monitor" / "mute.json"

_MENTION_RE = re.compile(r"@\w[\w_]*")
_UNMUTE_RE = re.compile(r"\bunmute\s+alerts\b", re.IGNORECASE)
_MUTE_RE = re.compile(r"\bmute\s+alerts\b", re.IGNORECASE)


@dataclass
class MuteState:
    muted: bool
    updated_at: str | None = None
    updated_by: str | None = None
    command: str | None = None
    chat_jid: str | None = None
    thread_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "muted": self.muted,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "command": self.command,
            "chat_jid": self.chat_jid,
            "thread_id": self.thread_id,
        }


def mute_file_path(nanoclaw_root: Path) -> Path:
    return nanoclaw_root / MUTE_REL_PATH


def _normalize_for_command(text: str) -> str:
    stripped = _MENTION_RE.sub(" ", text)
    return re.sub(r"\s+", " ", stripped).strip()


def parse_mute_command(text: str) -> MuteCommand | None:
    """Return mute/unmute if the message asks for it (trigger mentions ok)."""
    if not text or not str(text).strip():
        return None
    body = _normalize_for_command(str(text))
    # Unmute first so "unmute alerts" is not treated as mute.
    if _UNMUTE_RE.search(body):
        return "unmute"
    if _MUTE_RE.search(body):
        return "mute"
    return None


def read_mute_state(nanoclaw_root: Path) -> MuteState:
    path = mute_file_path(nanoclaw_root)
    if not path.is_file():
        return MuteState(muted=False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return MuteState(muted=False)
    if not isinstance(data, dict):
        return MuteState(muted=False)
    return MuteState(
        muted=bool(data.get("muted")),
        updated_at=(
            data.get("updated_at")
            if isinstance(data.get("updated_at"), str)
            else None
        ),
        updated_by=(
            data.get("updated_by")
            if isinstance(data.get("updated_by"), str)
            else None
        ),
        command=(
            data.get("command") if isinstance(data.get("command"), str) else None
        ),
        chat_jid=(
            data.get("chat_jid") if isinstance(data.get("chat_jid"), str) else None
        ),
        thread_id=(
            data.get("thread_id")
            if isinstance(data.get("thread_id"), str)
            else None
        ),
    )


def is_muted(nanoclaw_root: Path) -> bool:
    return read_mute_state(nanoclaw_root).muted


def write_mute_state(
    nanoclaw_root: Path,
    *,
    muted: bool,
    updated_by: str | None = None,
    command: MuteCommand | None = None,
    chat_jid: str | None = None,
    thread_id: str | None = None,
) -> MuteState:
    state = MuteState(
        muted=muted,
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z",
        updated_by=updated_by,
        command=command,
        chat_jid=chat_jid,
        thread_id=thread_id,
    )
    path = mute_file_path(nanoclaw_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return state
