#!/usr/bin/env python3
"""Hourly, silent upkeep of status entries in the knowledge store.

For each active status entry (and expired ones waiting in review):
  check holds            → extend expiry by ttl_days (stays active)
  check no longer holds  → auto-resolve with the observed value
  check errors           → leave it; once expired, queue for Spark's review
  no check and expired   → queue for Spark's review

Nothing is sent to chat here; kb_garden.py --review batches the review queue
into Spark's DM weekly.
"""

from __future__ import annotations

import argparse
import sys

from common import log, utcnow
import kb
import kb_check


def recheck(dry_run: bool = False) -> dict[str, int]:
    now = utcnow()
    counts = {"extended": 0, "resolved": 0, "review": 0, "unchanged": 0}
    changed: list[str] = []
    for entry in kb.list_entries():
        if entry.get("kind") != "status":
            continue
        state = entry.get("state")
        expired_review = state == "needs_review" and str(entry.get("review_reason", "")).startswith("expired")
        if state != "active" and not expired_review:
            continue
        expired = kb.is_expired(entry, now)
        before = kb.serialize_entry(entry)
        if entry.get("check"):
            result = kb_check.evaluate(entry)
            entry["last_check"] = result
            if result["result"] == "holds":
                kb.extend_expiry(entry, now)
                entry["state"] = "active"
                entry.pop("review_reason", None)
                counts["extended"] += 1
            elif result["result"] == "no_longer_holds":
                entry["state"] = "resolved"
                entry["resolution"] = f"auto-resolved {now.date()}: check no longer holds ({result.get('detail')})"
                entry.pop("review_reason", None)
                counts["resolved"] += 1
            elif expired and state == "active":
                entry["state"] = "needs_review"
                entry["review_reason"] = f"expired; check errored: {result.get('detail')}"
                counts["review"] += 1
            else:
                counts["unchanged"] += 1
        elif expired and state == "active":
            entry["state"] = "needs_review"
            entry["review_reason"] = "expired; no machine check — is this still true?"
            counts["review"] += 1
        else:
            counts["unchanged"] += 1
        if kb.serialize_entry(entry) != before:
            entry["updated"] = kb.iso_ms(now)
            changed.append(f"{entry['id']}→{entry['state']}")
            if not dry_run:
                kb.write_entry(entry)
    if changed and not dry_run:
        kb.commit(f"kb: hourly recheck ({', '.join(changed[:8])}{'…' if len(changed) > 8 else ''})")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    counts = recheck(args.dry_run)
    log(f"kb recheck: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
