"""Shared paths and helpers for Ghost host jobs (dictionary, change digest, knowledge store)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("NANOCLAW_ROOT", CODE_ROOT))
DATA_DIR = ROOT / "data"
GHOST_DIR = Path(os.environ.get("GHOST_DATA_DIR", DATA_DIR / "ghost"))
KNOWLEDGE_DIR = Path(os.environ.get("GHOST_KNOWLEDGE_DIR", DATA_DIR / "knowledge"))
CODEBASE_DIR = Path(os.environ.get("NANOCLAW_CODEBASE_DIR", ROOT / "codebase"))
STORE_DB = ROOT / "store" / "messages.db"
SHARED_DIR = ROOT / "groups" / "global" / "shared"
TELEMETRY_MCP_DIR = CODE_ROOT / "container" / "telemetry-mcp"

MCP_ENV_KEYS = (
    "TELEMETRY_S3_ACCESS_KEY",
    "TELEMETRY_S3_SECRET_KEY",
    "TELEMETRY_S3_REGION",
    "TELEMETRY_S3_ENDPOINT",
    "TELEMETRY_S3_BUCKET",
    "TELEMETRY_S3_BUCKETS",
    "GRAFANA_URL",
    "GRAFANA_SERVICE_ACCOUNT_TOKEN",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[ghost {iso(utcnow())}] {msg}", file=sys.stderr, flush=True)


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines (optional export, single/double quotes). No shell expansion."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def load_mcp_env() -> None:
    """Export telemetry/Grafana settings from secrets/mcp.env into os.environ (values never logged)."""
    values = parse_env_file(ROOT / "secrets" / "mcp.env")
    for key in MCP_ENV_KEYS:
        if key in values and not os.environ.get(key):
            os.environ[key] = values[key]


def import_store():
    """Import container/telemetry-mcp/store.py (needs duckdb + boto3 on this interpreter)."""
    load_mcp_env()
    sys.path.insert(0, str(TELEMETRY_MCP_DIR))
    import store  # noqa: E402

    return store


def git(repo: Path, *args: str, check: bool = True, timeout: int = 60) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo.name}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def git_show(repo: Path, ref: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_ls(repo: Path, ref: str, path: str) -> list[str]:
    out = git(repo, "ls-tree", "-r", "--name-only", ref, "--", path, check=False)
    return [line for line in out.splitlines() if line]


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def db_readonly() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{STORE_DB}?mode=ro", uri=True)


def main_group() -> dict[str, str] | None:
    """The registered main group (Spark's DM). Never hardcode its JID."""
    if not STORE_DB.exists():
        return None
    con = db_readonly()
    try:
        row = con.execute(
            "SELECT jid, folder, name FROM registered_groups WHERE is_main = 1 LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return {"jid": row[0], "folder": row[1], "name": row[2]}


def send_ipc_message(chat_jid: str, text: str, *, via_folder: str) -> Path:
    """Queue a plain message through NanoClaw's IPC watcher (main folder may target any chat)."""
    msg_dir = DATA_DIR / "ipc" / via_folder / "messages"
    msg_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(utcnow().timestamp() * 1000)
    path = msg_dir / f"{stamp}-ghost.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"type": "message", "chatJid": chat_jid, "text": text}) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
