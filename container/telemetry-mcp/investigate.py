"""Purpose-built investigation tools over the parquet archive.

Each returns a compact dict (bounded by MAX_TOOL_CHARS) with deterministic facts
so the agent reasons over evidence instead of hand-written SQL.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import signals as sigs
import store
from paths import parse_timestamp

MAX_TOOL_CHARS = 20_000
_WATCHDOG_RE = re.compile(r"watchdog '(\w+)'(?: \(priority \d+\))? (?:triggered|preempting)")
_RECOVERY_RE = re.compile(r"recovery '(\w+)'")


def _parse_when(value: str, default: datetime) -> datetime:
    if not value or not value.strip():
        return default
    return store.clamp_end(parse_timestamp(value))


def _ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return parse_timestamp(str(value))


def budget(payload: dict[str, Any], max_chars: int = MAX_TOOL_CHARS) -> dict[str, Any]:
    """Trim the longest lists until the JSON fits; record what was cut."""
    text = store.to_json(payload)
    trimmed: list[str] = []
    guard = 0
    while len(text) > max_chars and guard < 40:
        guard += 1
        longest_key, longest_len, parent = None, 0, None

        def scan(obj: Any, path: str) -> None:
            nonlocal longest_key, longest_len, parent
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, list) and len(v) > 4:
                        size = len(store.to_json(v))
                        if size > longest_len:
                            longest_key, longest_len, parent = k, size, obj
                    scan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj[:50]):
                    scan(v, f"{path}[{i}]")

        scan(payload, "")
        if parent is None:
            break
        items = parent[longest_key]
        keep = max(4, len(items) // 2)
        head, tail = items[: keep // 2], items[-(keep - keep // 2) :]
        parent[longest_key] = head + [{"…": f"{len(items) - keep} items omitted"}] + tail
        trimmed.append(longest_key)
        text = store.to_json(payload)
    if trimmed:
        payload["output_trimmed"] = sorted(set(trimmed))
        payload["output_note"] = "Output was trimmed to fit; narrow the time range or signals for full detail."
    return payload


def _source(s3, host: str, stream: str = "telemetry") -> store.Source:
    return store.resolve_source(s3, host, "control", stream)


# ---------------------------------------------------------------------------
# find_transitions
# ---------------------------------------------------------------------------


def find_transitions(
    host: str,
    signal_specs: list[str],
    start: str = "",
    end: str = "",
    max_events: int = 60,
    gap_seconds: float = 10.0,
) -> dict[str, Any]:
    s3 = store.s3_client()
    src = _source(s3, host)
    end_dt = _parse_when(end, store.now_utc())
    start_dt = parse_timestamp(start) if start else end_dt - timedelta(hours=6)
    if (end_dt - start_dt) > timedelta(days=7):
        raise ValueError("range too large (max 7 days); narrow start/end")
    resolved = [sigs.resolve(s, src.host) for s in signal_specs[:8]]
    from_sql = src.from_sql(s3, start_dt, end_dt)
    if from_sql is None:
        return {"host": src.host, "start": start_dt.isoformat(), "end": end_dt.isoformat(), "error": "no parquet objects in range"}

    # Floats without a condition change every tick; track only null/non-null edges + stats.
    edge_exprs = []
    for r in resolved:
        t = (r["signal"] or {}).get("type", "")
        if not r["is_condition"] and t == "float":
            edge_exprs.append((f"({r['expr']} IS NULL)", True))
        else:
            edge_exprs.append((r["expr"], False))

    cols = ", ".join(f"{e} AS v{i}" for i, (e, _) in enumerate(edge_exprs))
    lags = ", ".join(f"v{i}, lag(v{i}) OVER w AS p{i}" for i in range(len(edge_exprs)))
    changed = " OR ".join(f"(v{i} IS DISTINCT FROM p{i})" for i in range(len(edge_exprs)))
    sql = f"""
WITH base AS (
  SELECT timestamp, {cols} FROM {from_sql} WHERE {src.time_where(start_dt, end_dt)}
), l AS (
  SELECT timestamp, lag(timestamp) OVER w AS pt, {lags} FROM base WINDOW w AS (ORDER BY timestamp)
)
SELECT CAST(timestamp AS VARCHAR) AS ts, CAST(pt AS VARCHAR) AS prev_ts,
       epoch(timestamp) - epoch(pt) AS dt, {", ".join(f"v{i}, p{i}" for i in range(len(edge_exprs)))}
FROM l
WHERE pt IS NULL OR {changed} OR (epoch(timestamp) - epoch(pt)) > {float(gap_seconds)}
ORDER BY timestamp
LIMIT 20000
"""
    _, rows = store.execute(sql)
    initial: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    counts = {r["label"]: 0 for r in resolved}
    for row in rows:
        ts, prev_ts, dt = row[0], row[1], row[2]
        vals = row[3:]
        if prev_ts is None:
            for i, r in enumerate(resolved):
                v = vals[2 * i]
                initial[r["label"]] = ("null" if v else "non-null") if edge_exprs[i][1] else v
            continue
        if dt is not None and dt > gap_seconds:
            gaps.append({"from": prev_ts, "to": ts, "seconds": round(dt, 1)})
        for i, r in enumerate(resolved):
            v, p = vals[2 * i], vals[2 * i + 1]
            if v is None and p is None:
                continue
            if v != p or (v is None) != (p is None):
                counts[r["label"]] += 1
                if edge_exprs[i][1]:
                    events.append({"ts": ts, "signal": r["label"], "from": "null" if p else "value", "to": "null" if v else "value"})
                else:
                    events.append({"ts": ts, "signal": r["label"], "from": p, "to": v})

    stats = {}
    float_specs = [(i, r) for i, r in enumerate(resolved) if edge_exprs[i][1]]
    if float_specs:
        agg = ", ".join(
            f"min({r['expr']}) AS mn{i}, max({r['expr']}) AS mx{i}, avg({r['expr']}) AS av{i}, "
            f"count({r['expr']}) AS n{i}, arg_min({r['expr']}, timestamp) AS first{i}, arg_max({r['expr']}, timestamp) AS last{i}"
            for i, r in float_specs
        )
        _, srows = store.execute(f"SELECT count(*) AS total, {agg} FROM {from_sql} WHERE {src.time_where(start_dt, end_dt)}")
        srow = srows[0]
        idx = 1
        for i, r in float_specs:
            mn, mx, av, n, first, last = srow[idx : idx + 6]
            idx += 6
            stats[r["label"]] = {
                "min": mn,
                "max": mx,
                "avg": round(av, 3) if av is not None else None,
                "non_null_samples": n,
                "total_samples": srow[0],
                "first": first,
                "last": last,
            }

    limit = max(10, min(int(max_events), 300))
    shown = events if len(events) <= limit else events[: limit // 2] + [{"…": f"{len(events) - limit} events omitted"}] + events[-limit // 2 :]
    return budget(
        {
            "host": src.host,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "signals": [{"label": r["label"], "sql": r["expr"], "tracking": "null edges + stats" if edge_exprs[i][1] else "value edges"} for i, r in enumerate(resolved)],
            "initial": initial,
            "transition_counts": counts,
            "events": shown,
            "data_gaps": gaps[:40],
            "float_stats": stats or None,
            "note": "Floats without a comparison only report null↔value edges; add a condition like 'warm_tank_level < 14' for threshold crossings.",
        }
    )


# ---------------------------------------------------------------------------
# why_locked
# ---------------------------------------------------------------------------


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    else:
        out[prefix] = obj
    return out


def _signal_meta(path: str) -> dict[str, Any] | None:
    """path like 'airbed.controls.vent_or_bag_valve' or 'dacs.a.sensors.x' → dictionary entry."""
    parts = path.split(".")
    if parts and parts[0] == "dacs" and len(parts) > 2:
        parts = [p for i, p in enumerate(parts) if not (i == 1 or (i >= 1 and parts[i - 1] == "filters"))]
    return sigs.by_id(".".join(parts))


def _console_lines(s3, host: str, start: datetime, end: datetime, limit: int = 60) -> list[dict[str, Any]]:
    try:
        src = _source(s3, host, "console")
    except LookupError:
        return []
    from_sql = src.from_sql(s3, start, end)
    if from_sql is None:
        return []
    sql = (
        f"SELECT CAST(timestamp AS VARCHAR) AS ts, level, message FROM {from_sql} "
        f"WHERE {src.time_where(start, end)} AND (lower(level) IN ('warn','warning','error') "
        f"OR message ILIKE '%watchdog%' OR message ILIKE '%recovery%' OR message ILIKE '%interlock%' "
        f"OR message ILIKE '%lock%') ORDER BY timestamp LIMIT {int(limit)}"
    )
    _, rows = store.execute(sql)
    return [{"ts": r[0], "level": r[1], "message": r[2][:400] if r[2] else r[2]} for r in rows]


def _collapse_console(lines: list[dict[str, Any]], max_items: int = 25) -> list[dict[str, Any]]:
    """Group repeating messages so a flapping recovery loop doesn't eat the budget."""
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ln in lines:
        key = re.sub(r"\d+(\.\d+)?", "#", ln.get("message") or "")
        if key not in groups:
            groups[key] = {"first_ts": ln["ts"], "last_ts": ln["ts"], "level": ln["level"], "message": ln["message"], "count": 0}
            order.append(key)
        groups[key]["count"] += 1
        groups[key]["last_ts"] = ln["ts"]
        for st in sigs.stations_for_text(ln.get("message") or ""):
            label = f"{st['id']} {st['address']} ({st['kind']}{': ' + ', '.join(st['devices']) if st['devices'] else ''})"
            names = groups[key].setdefault("stations", [])
            if label not in names:
                names.append(label)
    return [groups[k] for k in order[:max_items]]


def _digest_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace(" ", "T").replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _riser_keys(io: dict[str, Any] | None) -> set[str]:
    if not io:
        return set()
    keys = set()
    if io.get("read"):
        keys.add(f"{io['station']}:{io['riser']}")
    if io.get("write"):
        keys.add(f"{io.get('write_station', io['station'])}:{io.get('write_riser', io['riser'])}")
    return keys


def changes_before(
    host: str, onset: datetime, fields: list[str] | None = None, hours: float = 72.0, primary: list[str] | None = None
) -> dict[str, Any]:
    """Code changes and controller restarts before a fault, ranked by overlap with the affected signals/risers.

    `primary` are the signals that directly caused the fault (e.g. a watchdog's stale inputs); remaps of
    those signals, then of their risers, outrank remaps that only share a riser with other null fields.
    """
    digest = sigs.load_digest()
    if not digest:
        return {"note": "change digest unavailable (host job scripts/ghost/change_digest.py has not run)"}
    generated = _digest_ts(digest["generated_at"])
    window_start = onset - timedelta(hours=hours)
    io_map = (digest.get("io_map") or {}).get(host, {}).get("signals", {})
    leaves = {f.split(".")[-1] for f in fields or []}
    affected_risers: set[str] = set()
    for leaf in leaves:
        affected_risers |= _riser_keys(io_map.get(leaf))
    primary_leaves = {f.split(".")[-1] for f in primary or []}
    primary_risers: set[str] = set()
    for leaf in primary_leaves:
        primary_risers |= _riser_keys(io_map.get(leaf))
    leaves |= primary_leaves
    affected_risers |= primary_risers

    on_affected, other_io = [], []
    for ch in digest.get("io_changes", []):
        if ch.get("host") != host:
            continue
        authored = _digest_ts(ch["authored"])
        if authored > onset:
            continue
        live = [r for r in ch.get("restarts_since_authored", []) if _digest_ts(r) <= onset]
        item = {
            "sha": ch["sha"],
            "branch": ch["branch"],
            "author": ch["author"],
            "authored": ch["authored"],
            "subject": ch["subject"],
            "remaps": ch.get("remaps", []),
            "excerpt": ch.get("excerpt", []),
            "restarts_after_commit_before_onset": live,
            "hours_before_onset": round((onset - authored).total_seconds() / 3600, 1),
        }
        remaps = [
            r
            for r in ch.get("remaps", [])
            if not (r.get("reverted_by") and _digest_ts(r["reverted_by"]["committed"]) <= onset)
        ]
        hit = [r for r in remaps if r["signal"] in leaves or (set(r.get("risers", [])) & affected_risers)]
        if hit and live:
            item["match_score"] = max(
                3 if r["signal"] in primary_leaves else 2 if set(r.get("risers", [])) & primary_risers else 1 for r in hit
            )
            item["matched"] = [
                {"signal": r["signal"], "risers": sorted(set(r.get("risers", [])) & affected_risers) or r.get("risers")}
                for r in hit
            ]
            on_affected.append(item)
        elif authored >= window_start:
            other_io.append(item)

    on_affected.sort(key=lambda i: (-i["match_score"], i["hours_before_onset"]))
    host_restarts = ((digest.get("restarts") or {}).get(host) or {}).get("restarts", [])
    restarts = [r["ts"] for r in host_restarts if window_start <= _digest_ts(r["ts"]) <= onset]
    deployed_ref = (digest.get("deployed_refs") or {}).get(host, "origin/main").replace("origin/", "")
    risky = []
    for c in (digest.get("commits") or {}).get("co3ntrol-rs", []):
        if deployed_ref in c.get("branches", []) and c.get("areas") and window_start <= _digest_ts(c["authored"]) <= onset:
            risky.append({"sha": c["sha"], "authored": c["authored"], "subject": c["subject"], "areas": c["areas"]})

    out: dict[str, Any] = {
        "onset": onset.isoformat(),
        "digest_generated_at": digest["generated_at"],
        "deployed_branch": deployed_ref,
        "io_changes_on_affected_risers": on_affected[:5],
        "other_io_changes": other_io[:5],
        "controller_restarts_before_onset": restarts[-10:],
        "risky_commits_on_deployed_branch": risky[:10],
    }
    if generated < onset:
        out["digest_note"] = "digest was generated before the fault; commits deployed after it are not listed"
    if on_affected:
        top = on_affected[0]
        by_sig = {r["signal"]: r for r in top["remaps"]}
        def remap_text(m: dict[str, Any]) -> str:
            r = by_sig[m["signal"]]
            text = f"`{m['signal']}` on {'/'.join(m['risers'])}: {r['before']} → {r['after']}"
            if r.get("coils"):
                text += f" ({'; '.join(r['coils'] + r.get('coil_warnings', []))})"
            if r.get("reverted_by"):
                text += f" [later REVERTED by {r['reverted_by']['sha']} at {r['reverted_by']['committed'][:16]}]"
            return text

        sigs_hit = "; ".join(remap_text(m) for m in top["matched"])
        out["assessment"] = (
            f"{top['sha']} ({top['author']}, {top['hours_before_onset']}h before onset: {top['subject']}) remapped {sigs_hit}, "
            f"the same signals/risers that failed here, and the controller restarted after it "
            f"(last {top['restarts_after_commit_before_onset'][-1][:19]}). Treat this deploy as the leading hypothesis "
            "until ruled out: say so, quote the remap, and ask whether the pin change matches the wiring. "
            "Do not conclude a hardware/comms fault without addressing it."
        )
        if len(on_affected) > 1:
            out["assessment"] += " Also on affected risers (weaker match): " + ", ".join(
                f"{i['sha']} {i['subject'][:70]}" for i in on_affected[1:3]
            )
    elif restarts or risky:
        out["assessment"] = (
            "No I/O remap touches the affected signals, but there were deploys/restarts before onset; "
            "check whether the fault started after one of them before blaming hardware."
        )
    return {"assessment": out.pop("assessment"), **out} if "assessment" in out else out


def recent_changes(host: str, at: str = "", signals: list[str] | None = None, hours: float = 72.0) -> dict[str, Any]:
    onset = _parse_when(at, store.now_utc())
    host = _source(store.s3_client(), host).host
    return budget({"host": host, **changes_before(host, onset, signals or [], max(1.0, min(float(hours), 336.0)))})


def why_locked(host: str, at: str = "", lookback_hours: float = 6.0) -> dict[str, Any]:
    s3 = store.s3_client()
    src = _source(s3, host)
    end_dt = _parse_when(at, store.now_utc())
    start_dt = end_dt - timedelta(hours=max(0.25, min(float(lookback_hours), 72.0)))
    from_sql = src.from_sql(s3, start_dt, end_dt)
    if from_sql is None:
        return {"host": src.host, "error": "no telemetry parquet in window", "start": start_dt.isoformat(), "end": end_dt.isoformat()}

    edges_sql = f"""
WITH base AS (
  SELECT timestamp, app.supervisor.lock AS lk, app.supervisor.active_recovery AS rec, app.systick AS st
  FROM {from_sql} WHERE {src.time_where(start_dt, end_dt)}
), l AS (
  SELECT *, lag(lk) OVER w AS plk, lag(st) OVER w AS pst, lag(timestamp) OVER w AS pt FROM base WINDOW w AS (ORDER BY timestamp)
)
SELECT CAST(timestamp AS VARCHAR), lk, plk, rec, st, pst, CAST(pt AS VARCHAR)
FROM l WHERE pt IS NULL OR (lk IS DISTINCT FROM plk) OR st < pst
ORDER BY timestamp LIMIT 5000
"""
    _, rows = store.execute(edges_sql)
    _, last_rows = store.execute(
        f"SELECT CAST(timestamp AS VARCHAR), app.supervisor.lock, app.supervisor.active_recovery, app.systick "
        f"FROM {from_sql} WHERE {src.time_where(start_dt, end_dt)} ORDER BY timestamp DESC LIMIT 1"
    )
    rising, falling, restarts = [], [], []
    initial_locked = None
    for ts, lk, plk, rec, st, pst, pt in rows:
        if pt is None:
            initial_locked = lk
            continue
        if st is not None and pst is not None and st < pst:
            restarts.append({"ts": ts, "systick_before": pst, "systick_after": st})
        if lk and not plk:
            rising.append({"ts": ts, "recovery_at_edge": rec, "systick": st})
        elif plk and not lk:
            falling.append({"ts": ts})

    last = last_rows[0] if last_rows else None
    result: dict[str, Any] = {
        "host": src.host,
        "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "latest_sample": {"ts": last[0], "locked": last[1], "active_recovery": last[2], "systick_s": last[3]} if last else None,
        "lock_rising_edges": rising[-10:],
        "lock_clears": falling[-10:],
        "restarts": restarts[-10:],
    }
    if not rising:
        if initial_locked:
            result["finding"] = "Already locked at the start of the window; increase lookback_hours to find the lock edge."
        else:
            result["finding"] = "No lock rising edge in the window."
        return budget(result)

    edge = rising[-1]
    edge_dt = _ts(edge["ts"])
    clear_after = next((f for f in falling if _ts(f["ts"]) > edge_dt), None)
    result["lock_edge"] = edge["ts"]
    result["lock_cleared"] = clear_after["ts"] if clear_after else None
    if edge.get("systick") is not None and edge["systick"] < 120:
        result["restart_note"] = f"systick={edge['systick']}s at the lock edge: controller restarted just before locking."

    # Full system state just before vs at the edge.
    w_start, w_end = edge_dt - timedelta(seconds=15), edge_dt + timedelta(seconds=20)
    win_from = src.from_sql(s3, w_start, w_end) or from_sql
    _, snap_rows = store.execute(
        f"SELECT CAST(timestamp AS VARCHAR), to_json(app.system), app.supervisor.active_recovery, "
        f"to_json(app.supervisor.triggered_interlocks) FROM {win_from} WHERE {src.time_where(w_start, w_end)} ORDER BY timestamp"
    )
    before = [r for r in snap_rows if _ts(r[0]) < edge_dt]
    at_edge = [r for r in snap_rows if _ts(r[0]) >= edge_dt]
    recoveries_after = [r[2] for r in at_edge if r[2]]
    interlocks_after = [r[3] for r in at_edge if r[3] and r[3] != "[]"]
    result["active_recovery_after_edge"] = sorted(set(recoveries_after))
    if interlocks_after:
        result["triggered_interlocks_after_edge"] = interlocks_after[:3]

    went_null: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    if before and at_edge:
        pre = _flatten(json.loads(before[0][1] or "{}"))
        post = _flatten(json.loads(at_edge[0][1] or "{}"))
        for path in sorted(set(pre) | set(post)):
            a, b = pre.get(path), post.get(path)
            if a is not None and b is None:
                meta = _signal_meta(path) or {}
                went_null.append({"field": path, "was": a, "station": meta.get("station"), "read_path": meta.get("read_path")})
            elif a != b and (isinstance(a, (bool, str)) or isinstance(b, (bool, str))):
                changed.append({"field": path, "from": a, "to": b})
        result["compared_samples"] = {"before": before[0][0], "at_edge": at_edge[0][0]}
    by_station: dict[str, int] = {}
    for n in went_null:
        key = n.get("station") or "unknown"
        by_station[key] = by_station.get(key, 0) + 1
    result["fields_went_null"] = went_null[:60]
    result["fields_went_null_by_station"] = by_station
    result["discrete_changes"] = changed[:40]

    # Console around the edge (log lines lag telemetry by a few seconds).
    console = _console_lines(s3, src.host, edge_dt - timedelta(seconds=30), edge_dt + timedelta(seconds=90), limit=120)
    triggered = []
    for ln in console:
        m = _WATCHDOG_RE.search(ln.get("message") or "")
        if m and m.group(1) not in triggered:
            triggered.append(m.group(1))
    result["console"] = _collapse_console(console)
    result["watchdogs_in_console"] = triggered

    # Evaluate candidate watchdog inputs at the edge.
    system = sigs.system_for_host(src.host) or "airbed"
    rules = [r for r in sigs.rules_for(system) if r["kind"] == "watchdog"]
    rec_names = set(recoveries_after)
    candidates = [r for r in rules if r["name"] in triggered] or [
        r for r in rules if rec_names & {ra.get("recovery") for ra in r.get("recovery_actions", [])}
    ] or rules
    evaluated = []
    for rule in candidates[:4]:
        inputs_eval = []
        for inp in rule.get("inputs", []):
            meta = sigs.by_id(inp["signal"])
            if meta is None or meta.get("dims"):
                inputs_eval.append({"signal": inp["signal"], "note": "per-instance input; inspect with find_transitions using a raw app.system.dacs.<id>… path"})
                continue
            n_back = int(inp.get("max_sample_age") or 2) + 3
            _, vrows = store.execute(
                f"SELECT CAST(timestamp AS VARCHAR), {meta['sql']} FROM {win_from} "
                f"WHERE {src.time_where(edge_dt - timedelta(seconds=60), edge_dt)} ORDER BY timestamp DESC LIMIT {n_back}"
            )
            values = [{"ts": r[0], "value": r[1]} for r in reversed(vrows)]
            newest_valid_age = next((i for i, r in enumerate(vrows) if r[1] is not None), None)
            entry: dict[str, Any] = {
                "signal": inp["signal"],
                "gui_label": meta.get("gui_label"),
                "accessor": inp["accessor"],
                "values_up_to_edge": values,
            }
            if inp.get("max_sample_age") is not None:
                limit_age = int(inp["max_sample_age"])
                entry["max_sample_age"] = limit_age
                entry["stale_at_edge"] = newest_valid_age is None or newest_valid_age >= limit_age
            inputs_eval.append(entry)
        stale = [i["signal"] for i in inputs_eval if i.get("stale_at_edge")]
        ev = {
            "rule": rule["name"],
            "system": rule["system"],
            "priority": rule.get("priority"),
            "params": rule.get("params"),
            "missing_input_trips": rule.get("missing_input_trips"),
            "recovery_actions": rule.get("recovery_actions"),
            "inputs": inputs_eval,
            "stale_inputs": stale,
            "check_source": (rule.get("source") or "")[:1800],
        }
        if stale and rule.get("missing_input_trips"):
            ev["assessment"] = (
                f"Inputs {stale} were missing/stale at the lock edge and this watchdog fires on missing inputs: "
                "the lock is explained by missing telemetry, not by an out-of-range process value. "
                "Find out why those fields went null: recent_changes (deploys/I/O remaps on the same riser) first, "
                "then fields_went_null_by_station and console errors."
            )
        evaluated.append(ev)
    result["watchdog_evaluation"] = evaluated

    affected = [n["field"] for n in went_null] + [s for ev in evaluated for s in ev.get("stale_inputs", [])]
    try:
        primary = [s for ev in evaluated for s in ev.get("stale_inputs", [])]
        result["recent_changes"] = changes_before(src.host, edge_dt, affected, primary=primary)
    except Exception as exc:  # digest is advisory; never fail the lock analysis
        result["recent_changes"] = {"error": str(exc)[:200]}

    summary = [f"Lock rose at {edge['ts']} on {src.host}."]
    if result["recent_changes"].get("io_changes_on_affected_risers"):
        top = result["recent_changes"]["io_changes_on_affected_risers"][0]
        summary.append(f"RECENT CHANGE ON AFFECTED I/O: {top['sha']} {top['subject'][:90]} (see change_check).")
    if triggered:
        summary.append(f"Console names watchdog(s): {', '.join(triggered)}.")
    if recoveries_after:
        summary.append(f"Recovery active after edge: {', '.join(sorted(set(recoveries_after)))}.")
    for ev in evaluated:
        if ev.get("assessment"):
            summary.append(f"{ev['rule']}: {ev['assessment']}")
    if went_null:
        summary.append(f"{len(went_null)} field(s) went null between samples, by station: {by_station}.")
    errors = [c for c in result["console"] if (c.get("level") or "").lower() == "error"]
    if errors:
        summary.append(f"Console errors near edge: {errors[0]['message']} (x{errors[0]['count']}).")
    summary.append(f"Lock cleared at {clear_after['ts']}." if clear_after else "Lock not cleared by the end of the window (needs clear_lock).")
    # Summary and change check first: evidence logs and readers truncate long outputs from the end.
    ordered = {"summary": summary, "change_check": result["recent_changes"].get("assessment") or "no recent changes found"}
    return budget({**ordered, **result})


# ---------------------------------------------------------------------------
# plant_snapshot
# ---------------------------------------------------------------------------


def plant_snapshot(host: str, at: str = "", include_console: bool = True) -> dict[str, Any]:
    s3 = store.s3_client()
    src = _source(s3, host)
    now = store.now_utc()
    at_dt = _parse_when(at, now)
    # Explicit `at`: the sample must be within 10 min. Default: newest archived sample (uploads lag).
    windows = [timedelta(minutes=10)] if at else [timedelta(hours=3), timedelta(days=2)]
    rows: list[tuple] = []
    for span in windows:
        start_dt = at_dt - span
        from_sql = src.from_sql(s3, start_dt, at_dt)
        if from_sql is None:
            continue
        _, rows = store.execute(
            f"SELECT CAST(timestamp AS VARCHAR), to_json(app) FROM {from_sql} WHERE {src.time_where(start_dt, at_dt)} "
            f"ORDER BY timestamp DESC LIMIT 1"
        )
        if rows:
            break
    if not rows:
        loc_day = store.latest_partition_day(s3, "control", "telemetry", src.host, src.bucket, src.layout)
        return {
            "host": src.host,
            "at": at_dt.isoformat(),
            "error": "no archived telemetry near `at` (archive uploads lag up to ~1h; use Grafana for live)",
            "latest_partition_day": str(loc_day),
        }
    ts, app_json = rows[0]
    app = json.loads(app_json)
    sample_dt = _ts(ts)
    sup = app.get("supervisor") or {}
    system = app.get("system") or {}
    out: dict[str, Any] = {
        "host": src.host,
        "sample_ts": ts,
        "sample_age_s": round((at_dt - sample_dt).total_seconds(), 1),
        "uptime_s": app.get("systick"),
        "supervisor": {
            "lock": sup.get("lock"),
            "active_recovery": sup.get("active_recovery"),
            "triggered_interlocks": sup.get("triggered_interlocks") or [],
        },
    }
    sections: dict[str, dict[str, Any]] = {}
    nulls: dict[str, list[str]] = {}
    flat = _flatten(system)
    for path, val in sorted(flat.items()):
        meta = _signal_meta(path) or {}
        label = meta.get("gui_label") or path.rsplit(".", 1)[-1]
        segs = path.split(".")
        if segs[0] == "dacs" and len(segs) > 2:
            inst = f"dac {segs[1]}" + (f" filter {segs[3]}" if len(segs) > 4 and segs[2] == "filters" else "")
            label = f"{inst}: {label}"
        section = meta.get("gui_section") or meta.get("group") or path.split(".")[0]
        kind = "controls" if ".controls." in path else "sensors"
        bucket = sections.setdefault(f"{path.split('.')[0]} / {section}", {"on": [], "values": {}})
        if val is None:
            if meta.get("wired") is not False:
                nulls.setdefault(meta.get("station") or path.split(".")[0], []).append(label)
            continue
        if isinstance(val, bool):
            if val and kind == "controls":
                bucket["on"].append(label)
            elif kind == "sensors":
                bucket["values"][label] = val
            continue
        unit = meta.get("units", "")
        unit = "" if unit.startswith("bool") or "unverified" in unit else unit
        bucket["values"][label] = f"{round(val, 3) if isinstance(val, float) else val}{(' ' + unit) if unit else ''}"
    out["sections"] = {k: v for k, v in sections.items() if v["on"] or v["values"]}
    out["null_fields_by_station"] = nulls
    if include_console:
        out["recent_console"] = _collapse_console(
            _console_lines(s3, src.host, sample_dt - timedelta(minutes=15), sample_dt, limit=200), max_items=15
        )
    out["note"] = (
        "Archive snapshot (S3 parquet uploads lag up to ~1h). Controls list = commanded/readback ON. "
        "For live values use Grafana Prom with lookup_signal(...).prom."
    )
    return budget(out)
