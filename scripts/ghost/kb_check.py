#!/usr/bin/env python3
"""Evaluate a status entry's machine check.

Check types (see src/knowledge.ts StatusCheck):
  stream_fresh  newest telemetry object for `host` is younger than max_age_hours
  s3_sql        DuckDB query over the plant archive returning one scalar, compared op/value
  prom          PromQL instant query via the Grafana datasource proxy, compared op/value

The check's boolean is compared with `holds_when` (default true): equal means the
status text is still true ("holds"), different means "no_longer_holds".

  kb_check.py --entry k-... [--record]   evaluate one entry (record = write last_check)
"""

from __future__ import annotations

import argparse
import json
import operator
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from common import import_store, load_mcp_env, log, utcnow
import kb

OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


def _compare(observed: Any, op: str, expected: Any) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        observed = float(observed)
    elif isinstance(expected, bool):
        observed = observed in (True, 1, "1", "true", "True")
    return bool(OPS.get(op or "==", operator.eq)(observed, expected))


def _stream_fresh(check: dict[str, Any]) -> tuple[bool, Any, str]:
    store = import_store()
    s3 = store.s3_client()
    host = check["host"]
    stream = check.get("stream") or "telemetry"
    loc = store.find_location(s3, host, "control", stream)
    if not loc:
        return False, None, f"no {stream} stream for {host} in any bucket"
    day = store.latest_partition_day(s3, "control", stream, host, loc["bucket"], loc["layout"])
    if day is None:
        return False, None, f"no partitions for {host}"
    items = store.list_keys(
        s3, store.hive_prefix("control", stream, host, day, layout=loc["layout"]), 500, loc["bucket"]
    )
    if not items:
        return False, None, f"empty partition {day} for {host}"
    newest = max(datetime.fromisoformat(i["last_modified"]) for i in items)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age_h = (utcnow() - newest).total_seconds() / 3600
    max_age = float(check.get("max_age_hours") or 3)
    return age_h < max_age, round(age_h, 2), f"newest {host} {stream} object {age_h:.1f}h old (limit {max_age}h)"


def _s3_sql(check: dict[str, Any]) -> tuple[bool, Any, str]:
    store = import_store()
    result = store.run_sql(check["sql"], 5)
    rows = result.get("rows") or []
    if not rows:
        return False, None, "query returned no rows"
    first = rows[0]
    observed = next(iter(first.values())) if isinstance(first, dict) else first[0]
    ok = _compare(observed, check.get("op", "=="), check.get("value"))
    return ok, observed, f"sql → {observed!r} {check.get('op', '==')} {check.get('value')!r}"


def _prom(check: dict[str, Any]) -> tuple[bool, Any, str]:
    load_mcp_env()
    base = os.environ.get("GRAFANA_URL", "").rstrip("/")
    token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
    if not base or not token:
        raise RuntimeError("GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN not configured")
    uid = os.environ.get("PROM_DATASOURCE_UID", "grafanacloud-prom")
    url = f"{base}/api/datasources/proxy/uid/{uid}/api/v1/query?" + urllib.parse.urlencode({"query": check["query"]})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    data = payload.get("data") or {}
    result = data.get("result")
    if data.get("resultType") == "scalar":
        observed: Any = float(result[1])
    elif isinstance(result, list) and result:
        observed = float(result[0]["value"][1])
    else:
        return False, None, "query returned no series"
    ok = _compare(observed, check.get("op", "=="), check.get("value"))
    return ok, observed, f"prom → {observed} {check.get('op', '==')} {check.get('value')!r}"


RUNNERS = {"stream_fresh": _stream_fresh, "s3_sql": _s3_sql, "prom": _prom}


def evaluate(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a CheckResult dict: {at, result: holds|no_longer_holds|error, observed, detail}."""
    check = entry.get("check") or {}
    at = kb.iso_ms(utcnow())
    runner = RUNNERS.get(check.get("type", ""))
    if runner is None:
        return {"at": at, "result": "error", "detail": f"unknown check type {check.get('type')!r}"}
    try:
        value, observed, detail = runner(check)
    except Exception as err:  # noqa: BLE001 - surface any failure as a check error
        return {"at": at, "result": "error", "detail": f"{type(err).__name__}: {str(err)[:300]}"}
    holds_when = check.get("holds_when", True)
    result = "holds" if bool(value) == bool(holds_when) else "no_longer_holds"
    return {"at": at, "result": result, "observed": observed, "detail": detail[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entry", required=True)
    ap.add_argument("--record", action="store_true", help="write last_check into the entry and commit")
    args = ap.parse_args()
    entry = kb.read_entry(args.entry)
    if entry is None:
        log(f"no entry {args.entry}")
        return 2
    if not entry.get("check"):
        log(f"{args.entry} has no check")
        return 2
    result = evaluate(entry)
    print(json.dumps(result, default=str))
    if args.record:
        entry["last_check"] = result
        kb.write_entry(entry)
        kb.commit(f"kb: check {args.entry} → {result['result']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
