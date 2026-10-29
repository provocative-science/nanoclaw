#!/usr/bin/env python3
"""Knowledge store gardener.

Daily (default): find likely duplicates, open contradictions, stale unverified
claims and long-untouched references; write data/ghost/review-queue.json. Read-only
on entries.

Weekly (--review): send the queue to Spark's DM (the registered main group, never
a group chat) when it is non-empty. Spark replies `still k-…`, `resolved k-…`,
`confirm k-…` or `retract k-…`; NanoClaw applies those without an agent run.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import timedelta
from typing import Any

from common import GHOST_DIR, log, main_group, send_ipc_message, utcnow, write_json_atomic
import kb

QUEUE_PATH = GHOST_DIR / "review-queue.json"
MAX_MESSAGE_ITEMS = 15
STOP = {"the", "and", "for", "with", "was", "are", "not", "this", "that", "from", "into", "when"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]{3,}", text.lower()) if t not in STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def build_queue() -> list[dict[str, Any]]:
    now = utcnow()
    entries = kb.list_entries()
    live = [e for e in entries if e.get("state") in ("active", "needs_review")]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(entry: dict[str, Any], reason: str, related: list[str] | None = None) -> None:
        if entry["id"] in seen:
            return
        seen.add(entry["id"])
        items.append(
            {
                "id": entry["id"],
                "kind": entry.get("kind"),
                "title": entry.get("title"),
                "confidence": entry.get("confidence"),
                "reason": reason,
                "related": related or [],
            }
        )

    for e in live:
        if e.get("state") == "needs_review":
            add(e, e.get("review_reason") or "flagged for review", e.get("contradicts"))

    for i, a in enumerate(live):
        ta = _tokens(a.get("title", ""))
        ba = _tokens(a.get("body", ""))
        for b in live[i + 1 :]:
            if a.get("kind") != b.get("kind"):
                continue
            title_sim = _jaccard(ta, _tokens(b.get("title", "")))
            body_sim = _jaccard(ba, _tokens(b.get("body", "")))
            shared_tags = len(set(a.get("tags", [])) & set(b.get("tags", [])))
            if title_sim >= 0.6 or (shared_tags >= 2 and body_sim >= 0.5):
                newer, older = sorted((a, b), key=lambda e: e.get("updated", ""), reverse=True)
                add(newer, f"possible duplicate of {older['id']} — supersede or retract one", [older["id"]])

    for e in live:
        if e.get("state") != "active":
            continue
        updated = kb.parse_time(e.get("updated")) or now
        age = now - updated
        if e.get("confidence") == "unverified" and age > timedelta(days=30):
            add(e, f"unverified for {age.days} days — confirm or retract")
        elif e.get("kind") in ("fact", "lesson", "note") and age > timedelta(days=180):
            add(e, f"not touched in {age.days} days — still accurate?")
    return items


def format_review(items: list[dict[str, Any]]) -> str:
    lines = [f"Knowledge review: {len(items)} item{'s' if len(items) != 1 else ''} need a decision."]
    for n, item in enumerate(items[:MAX_MESSAGE_ITEMS], 1):
        related = f" (see {', '.join(item['related'])})" if item.get("related") else ""
        lines.append(f"\n{n}. {item['id']} [{item['kind']}, {item['confidence']}] {item['title']}")
        lines.append(f"   {item['reason']}{related}")
    if len(items) > MAX_MESSAGE_ITEMS:
        lines.append(f"\n…and {len(items) - MAX_MESSAGE_ITEMS} more next week.")
    lines.append(
        "\nReply one per line: still <id> · resolved <id> · confirm <id> · retract <id> "
        "(optionally followed by a reason)."
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--review", action="store_true", help="send the queue to Spark's DM if non-empty")
    ap.add_argument("--dry-run", action="store_true", help="print instead of writing/sending")
    args = ap.parse_args()

    items = build_queue()
    payload = {"generated": kb.iso_ms(utcnow()), "items": items}
    if args.dry_run:
        print(format_review(items) if items else "(review queue empty)")
        return 0
    write_json_atomic(QUEUE_PATH, payload)
    log(f"kb garden: {len(items)} review item(s)")
    if not args.review or not items:
        return 0
    target = main_group()
    if not target:
        log("no main group registered; review not sent")
        return 1
    send_ipc_message(target["jid"], format_review(items), via_folder=target["folder"])
    log(f"kb review sent to main group {target['folder']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
