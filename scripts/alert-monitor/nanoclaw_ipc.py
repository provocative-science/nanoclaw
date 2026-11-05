"""Write NanoClaw filesystem IPC schedule_task payloads."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def schedule_once(
    *,
    nanoclaw_root: Path,
    ipc_group_folder: str,
    target_jid: str,
    prompt: str,
    task_id: str | None = None,
    thread_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a one-shot schedule_task under data/ipc/{folder}/tasks/.

    NanoClaw's IPC watcher picks this up and creates a SQLite scheduled task
    that runs promptly (schedule_value = now).
    """
    now = datetime.now().astimezone()
    # Local timestamp without Z — ipc.ts parses with Date(); ISO with offset works.
    schedule_value = now.isoformat(timespec="seconds")
    tid = task_id or f"plant-alert-{int(time.time())}-{int(time.time() * 1000) % 1000}"

    # context_mode=group: resume the chat's Claude session so the injected
    # alert prompt and Ghost's reply stay in the context window for follow-ups.
    payload: dict[str, Any] = {
        "type": "schedule_task",
        "taskId": tid,
        "prompt": prompt,
        "schedule_type": "once",
        "schedule_value": schedule_value,
        "context_mode": "group",
        "targetJid": target_jid,
        "model": "investigation",
    }
    if thread_id:
        payload["threadId"] = thread_id

    tasks_dir = nanoclaw_root / "data" / "ipc" / ipc_group_folder / "tasks"
    filename = f"{int(time.time())}-plant-alert.json"
    path = tasks_dir / filename

    if dry_run:
        return {
            "dry_run": True,
            "path": str(path),
            "payload": payload,
            "task_id": tid,
        }

    tasks_dir.mkdir(parents=True, exist_ok=True)
    # Write atomically via temp + rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {
        "dry_run": False,
        "path": str(path),
        "payload": payload,
        "task_id": tid,
    }


def send_message(
    *,
    nanoclaw_root: Path,
    ipc_group_folder: str,
    target_jid: str,
    text: str,
    thread_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Post a plain line to the chat via data/ipc/{folder}/messages/ (no agent run)."""
    payload: dict[str, Any] = {"type": "message", "chatJid": target_jid, "text": text}
    if thread_id:
        payload["threadId"] = thread_id
    messages_dir = nanoclaw_root / "data" / "ipc" / ipc_group_folder / "messages"
    path = messages_dir / f"{time.time_ns()}-plant-alert-line.json"
    if dry_run:
        return {"dry_run": True, "path": str(path), "payload": payload}
    messages_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {"dry_run": False, "path": str(path), "payload": payload}
