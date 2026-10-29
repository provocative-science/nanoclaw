"""Knowledge store entries for host jobs. Same format as src/knowledge.ts:
frontmatter with one `key: <JSON>` line per field, then a markdown body."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import KNOWLEDGE_DIR, log, utcnow

ENTRIES_DIR = KNOWLEDGE_DIR / "entries"
ID_RE = re.compile(r"^k-[\w-]+$")
FIELD_ORDER = (
    "id",
    "kind",
    "title",
    "tags",
    "systems",
    "state",
    "confidence",
    "author",
    "source",
    "created",
    "updated",
    "expires",
    "ttl_days",
    "check",
    "last_check",
    "supersedes",
    "superseded_by",
    "contradicts",
    "evidence",
    "resolution",
    "review_reason",
)
DEFAULT_STATUS_TTL_DAYS = 7


def iso_ms(dt: datetime) -> str:
    """Match JavaScript Date.toISOString()."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_entry(text: str) -> dict[str, Any]:
    m = re.match(r"^---\n([\s\S]*?)\n---\n?([\s\S]*)$", text)
    if not m:
        raise ValueError("missing frontmatter")
    entry: dict[str, Any] = {}
    for line in m.group(1).split("\n"):
        if not line.strip():
            continue
        key, sep, raw = line.partition(":")
        if not sep:
            raise ValueError(f"bad frontmatter line: {line}")
        raw = raw.strip()
        try:
            entry[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            entry[key.strip()] = raw
    entry["body"] = m.group(2).strip()
    return entry


def serialize_entry(entry: dict[str, Any]) -> str:
    lines = ["---"]
    for key in FIELD_ORDER:
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, list) and not value and key != "tags":
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")
    lines += ["---", "", str(entry.get("body", "")).strip(), ""]
    return "\n".join(lines)


def entry_path(entry_id: str) -> Path:
    if not ID_RE.match(entry_id):
        raise ValueError(f"invalid entry id: {entry_id}")
    return ENTRIES_DIR / f"{entry_id}.md"


def read_entry(entry_id: str) -> dict[str, Any] | None:
    p = entry_path(entry_id)
    return parse_entry(p.read_text(encoding="utf-8")) if p.exists() else None


def list_entries() -> list[dict[str, Any]]:
    if not ENTRIES_DIR.exists():
        return []
    out = []
    for p in sorted(ENTRIES_DIR.glob("*.md")):
        try:
            out.append(parse_entry(p.read_text(encoding="utf-8")))
        except ValueError as err:
            log(f"skipping unparseable {p.name}: {err}")
    return out


def write_entry(entry: dict[str, Any]) -> None:
    p = entry_path(entry["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(serialize_entry(entry), encoding="utf-8")
    tmp.replace(p)


def extend_expiry(entry: dict[str, Any], now: datetime | None = None) -> None:
    now = now or utcnow()
    ttl = float(entry.get("ttl_days") or DEFAULT_STATUS_TTL_DAYS)
    entry["expires"] = iso_ms(now + timedelta(days=ttl))


def is_expired(entry: dict[str, Any], now: datetime | None = None) -> bool:
    exp = parse_time(entry.get("expires"))
    return exp is not None and exp <= (now or utcnow())


def commit(message: str) -> None:
    """git add + commit in the knowledge repo; retries while the host holds index.lock."""
    if not (KNOWLEDGE_DIR / ".git").exists():
        log("knowledge store is not a git repo yet (NanoClaw initializes it at startup)")
        return
    for attempt in range(6):
        add = subprocess.run(["git", "-C", str(KNOWLEDGE_DIR), "add", "-A"], capture_output=True, text=True)
        status = subprocess.run(
            ["git", "-C", str(KNOWLEDGE_DIR), "status", "--porcelain"], capture_output=True, text=True
        )
        if add.returncode == 0 and not status.stdout.strip():
            return
        proc = subprocess.run(
            ["git", "-C", str(KNOWLEDGE_DIR), "commit", "-q", "-m", message], capture_output=True, text=True
        )
        if proc.returncode == 0:
            return
        if "index.lock" not in (proc.stderr + add.stderr) or attempt == 5:
            log(f"knowledge commit failed: {(proc.stderr or add.stderr).strip()[:200]}")
            return
        time.sleep(0.5 * (attempt + 1))
