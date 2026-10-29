#!/usr/bin/env python3
"""What changed recently: code (all active branches), restarts per host, operator chat.

Also fast-forwards the mounted codebase repos so agents read current code.
Writes data/ghost/change-digest.{json,md}; the host briefing injects the markdown
into every plant investigation.

Run: uv run --with duckdb --with boto3 python scripts/ghost/change_digest.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

HOSTS = ("airbed-host", "bop-host")
WINDOW_HOURS = int(os.environ.get("GHOST_DIGEST_HOURS", "48"))
RISKY_PATHS = (
    ("control/src/supervisor/", "supervisor rules/engine"),
    ("models/src/telemetry", "telemetry model (field names)"),
    ("control/src/system/", "IO / device drivers"),
    ("control/src/drivers/", "IO / device drivers"),
    ("jobs/", "automation jobs"),
    ("config/", "config"),
    ("alloy/", "metrics pipeline"),
)
PLANT_WORDS = re.compile(
    r"\b(lock(ed)?|unlock|watchdog|interlock|recovery|restart(ed)?|deploy(ed)?|reboot|valve|pump|compressor|"
    r"blower|vfd|pressure|psi|vacuum|temp(erature)?|tank|level|leak|vent|bag|lockhopper|elevator|sorbent|"
    r"steam|chiller|airbed|container|liquefaction|liq|tower|co2|telemetry|alarm|alert|trip(ped)?|fault|"
    r"modbus|riser|backplane|panel|sensor|calibrat\w*|maintenance|shut ?down|start ?up|e-?stop)\b",
    re.I,
)
MAX_CHAT_MESSAGES = 40


# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------


def sync_repo(repo: Path) -> dict[str, Any]:
    """git fetch, then fast-forward the checked-out branch when clean. Never rewrites local work."""
    status: dict[str, Any] = {"repo": repo.name}
    common.git(repo, "fetch", "--prune", "-q", check=False, timeout=120)
    branch = common.git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    status["branch"] = branch
    upstream = common.git(repo, "rev-parse", "--abbrev-ref", "@{u}", check=False).strip()
    if not upstream:
        status["pull"] = "no upstream"
        return status
    dirty = common.git(repo, "status", "--porcelain", "--untracked-files=no", check=False).strip()
    if dirty:
        status["pull"] = "skipped: local modifications"
        return status
    before = common.git(repo, "rev-parse", "HEAD", check=False).strip()
    try:
        common.git(repo, "merge", "--ff-only", "-q", "@{u}", timeout=60)
    except RuntimeError as exc:
        status["pull"] = f"skipped: {str(exc)[:160]}"
        return status
    after = common.git(repo, "rev-parse", "HEAD", check=False).strip()
    status["pull"] = "fast-forwarded" if before != after else "up to date"
    return status


def recent_commits(repo: Path, since: datetime) -> list[dict[str, Any]]:
    out = common.git(
        repo,
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)\t%(committerdate:iso-strict)",
        "refs/remotes/origin",
        check=False,
    )
    branches = []
    for line in out.splitlines():
        name, when = line.split("\t")
        if name in ("origin/HEAD", "origin"):
            continue
        if datetime.fromisoformat(when) >= since:
            branches.append(name)
    commits: dict[str, dict[str, Any]] = {}
    for ref in branches:
        rng = [ref] if ref == "origin/main" else [ref, "--not", "origin/main"]
        log = common.git(
            repo,
            "log",
            *rng,
            f"--since={since.isoformat()}",
            "--no-merges",
            "--format=%x1e%H%x1f%cI%x1f%an%x1f%s",
            "--name-only",
            check=False,
        )
        for block in log.split("\x1e"):
            if not block.strip():
                continue
            head, *files = block.strip().split("\n")
            sha, when, author, subject = head.split("\x1f")
            files = [f for f in files if f.strip()]
            entry = commits.setdefault(
                sha,
                {
                    "sha": sha[:9],
                    "when": when,
                    "author": author,
                    "subject": subject[:160],
                    "branches": [],
                    "files": len(files),
                    "areas": sorted({label for prefix, label in RISKY_PATHS if any(f.startswith(prefix) for f in files)}),
                    "sample_files": files[:6],
                },
            )
            entry["branches"].append(ref.replace("origin/", ""))
    return sorted(commits.values(), key=lambda c: c["when"], reverse=True)


# ---------------------------------------------------------------------------
# Restarts (systick resets in the parquet archive)
# ---------------------------------------------------------------------------


def restarts(since: datetime) -> dict[str, Any]:
    try:
        store = common.import_store()
        s3 = store.s3_client()
    except Exception as exc:  # duckdb/boto3 missing or no creds
        return {"error": f"telemetry unavailable: {exc}"}
    out: dict[str, Any] = {}
    end = datetime.now(timezone.utc)
    for host in HOSTS:
        try:
            src = store.resolve_source(s3, host)
            from_sql = src.from_sql(s3, since, end)
            if from_sql is None:
                out[host] = {"note": "no telemetry in window"}
                continue
            _, rows = store.execute(
                f"""
WITH b AS (SELECT timestamp, app.systick AS st FROM {from_sql} WHERE {src.time_where(since, end)}),
l AS (SELECT timestamp, st, lag(st) OVER (ORDER BY timestamp) AS pst, lag(timestamp) OVER (ORDER BY timestamp) AS pt FROM b)
SELECT CAST(timestamp AS VARCHAR), st, pst, CAST(pt AS VARCHAR), epoch(timestamp) - epoch(pt) FROM l
WHERE st < pst OR epoch(timestamp) - epoch(pt) > 300 ORDER BY timestamp
"""
            )
            _, last = store.execute(
                f"SELECT CAST(max(timestamp) AS VARCHAR), arg_max(app.systick, timestamp) FROM {from_sql} WHERE {src.time_where(since, end)}"
            )
            out[host] = {
                "restarts": [{"ts": r[0], "uptime_before_s": r[2]} for r in rows if r[1] is not None and r[2] is not None and r[1] < r[2]][:20],
                "gaps_over_5min": [{"from": r[3], "to": r[0], "minutes": round((r[4] or 0) / 60, 1)} for r in rows if r[4] and r[4] > 300][:20],
                "latest_sample": last[0][0] if last else None,
                "uptime_s": last[0][1] if last else None,
            }
        except Exception as exc:
            out[host] = {"error": str(exc)[:200]}
    return out


# ---------------------------------------------------------------------------
# Operator chat (group chats only — never DMs)
# ---------------------------------------------------------------------------


def operator_chat(since: datetime) -> list[dict[str, Any]]:
    if not common.STORE_DB.exists():
        return []
    con = common.db_readonly()
    try:
        rows = con.execute(
            """
SELECT c.name, m.sender_name, m.timestamp, m.content
FROM messages m JOIN chats c ON c.jid = m.chat_jid
JOIN registered_groups g ON g.jid = m.chat_jid
WHERE c.is_group = 1 AND m.is_bot_message = 0 AND m.is_from_me = 0 AND m.timestamp >= ?
ORDER BY m.timestamp DESC LIMIT 2000
""",
            (since.strftime("%Y-%m-%dT%H:%M:%S"),),
        ).fetchall()
    finally:
        con.close()
    out = []
    for chat, sender, ts, content in rows:
        text = (content or "").strip()
        if not text or not PLANT_WORDS.search(text):
            continue
        out.append({"chat": chat, "sender": sender, "ts": ts, "text": re.sub(r"\s+", " ", text)[:280]})
        if len(out) >= MAX_CHAT_MESSAGES:
            break
    return list(reversed(out))


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_md(d: dict[str, Any], max_commits: int = 12, max_chat: int = MAX_CHAT_MESSAGES) -> str:
    lines = [f"## Recent changes (last {d['window_hours']}h, generated {d['generated_at']})", ""]
    lines.append("### Code (co3ntrol-rs / co3ntrol-gui, all active branches)")
    any_commit = False
    for repo, commits in d["commits"].items():
        for c in commits[:max_commits]:
            any_commit = True
            areas = f" — touches {', '.join(c['areas'])}" if c["areas"] else ""
            lines.append(f"- {c['when'][:16]} {repo}@{','.join(c['branches'][:3])} {c['sha']} {c['author']}: {c['subject']}{areas}")
    if not any_commit:
        lines.append("- no commits")
    lines += ["", "### Controller restarts / telemetry gaps (archive)"]
    r = d["restarts"]
    if "error" in r:
        lines.append(f"- unavailable: {r['error']}")
    else:
        for host, info in r.items():
            if "error" in info or "note" in info:
                lines.append(f"- {host}: {info.get('error') or info.get('note')}")
                continue
            rs = ", ".join(x["ts"][:19] for x in info["restarts"]) or "none"
            gaps = ", ".join(f"{g['from'][:16]}→{g['to'][11:16]} ({g['minutes']}m)" for g in info["gaps_over_5min"][:5]) or "none"
            up = info.get("uptime_s")
            lines.append(
                f"- {host}: restarts {rs}; gaps>5m {gaps}; uptime {round(up / 3600, 1) if up else '?'}h at {str(info.get('latest_sample'))[:19]}"
            )
    lines += ["", "### Operator chat mentioning the plant (group chats)"]
    if not d["chat"]:
        lines.append("- none")
    for m in d["chat"][-max_chat:]:
        lines.append(f"- {m['ts'][:16]} [{m['chat']}] {m['sender']}: {m['text']}")
    lines += ["", "### Repo sync"]
    for s in d["repo_sync"]:
        lines.append(f"- {s['repo']} ({s.get('branch')}): {s.get('pull')}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-pull", action="store_true", help="fetch only; do not fast-forward working trees")
    ap.add_argument("--no-telemetry", action="store_true")
    args = ap.parse_args()
    now = common.utcnow()
    since = now - timedelta(hours=WINDOW_HOURS)
    repo_sync, commits = [], {}
    if common.CODEBASE_DIR.exists():
        for repo in sorted(p for p in common.CODEBASE_DIR.iterdir() if (p / ".git").exists()):
            if args.no_pull:
                common.git(repo, "fetch", "--prune", "-q", check=False, timeout=120)
                repo_sync.append({"repo": repo.name, "pull": "fetch only"})
            else:
                repo_sync.append(sync_repo(repo))
            if repo.name in ("co3ntrol-rs", "co3ntrol-gui"):
                commits[repo.name] = recent_commits(repo, since)
    digest = {
        "generated_at": common.iso(now),
        "window_hours": WINDOW_HOURS,
        "repo_sync": repo_sync,
        "commits": commits,
        "restarts": {"error": "skipped (--no-telemetry)"} if args.no_telemetry else restarts(since),
        "chat": operator_chat(since),
    }
    common.write_json_atomic(common.GHOST_DIR / "change-digest.json", digest)
    common.write_text_atomic(common.GHOST_DIR / "change-digest.md", render_md(digest))
    common.write_text_atomic(common.GHOST_DIR / "change-digest-brief.md", render_md(digest, max_commits=6, max_chat=12))
    common.log(f"change digest: {sum(len(v) for v in commits.values())} commits, {len(digest['chat'])} chat msgs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
