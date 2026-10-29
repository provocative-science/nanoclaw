#!/usr/bin/env python3
"""Stdio MCP server: query co3ntrol-rs parquet telemetry/logs in Wasabi S3."""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import investigate
import signals as sigs
import store
from paths import hive_prefix, parse_timestamp, parquet_s3_url, resolve_host
from store import (
    BUCKETS,
    ENDPOINT,
    default_range as _default_range,
    find_location as _find_location,
    inventory as _inventory,
    keys_for_range as _keys_for_range,
    known_hosts as _walk_hosts,
    list_keys as _list_keys,
    now_utc,
    read_parquet_sql as _read_parquet_sql,
    run_sql as _run_sql,
    s3_client as _s3,
    to_json as _json,
)

mcp = FastMCP("plant-telemetry")


@mcp.tool()
def list_streams() -> str:
    """List parquet streams across configured plant telemetry buckets.

    Two hive layouts:
      nested (airbed / beef): logs/{control|automation}/{telemetry|console}/host={host}/...
      machine (container-ship / a7f3): telemetry/machine={host}/... and logs/machine={host}/...
    Call this first. Container-ship is host=bop-host in provocative-dac-dev-a7f3.
    Airbed JSON is host=airbed-host in provocative-dac-dev-beef. Liquefaction (lichost)
    is not piped yet.
    """
    try:
        streams = _inventory(_s3())
        return _json(
            {
                "buckets": BUCKETS,
                "endpoint": ENDPOINT,
                "streams": streams,
                "aliases": {
                    "airbed": "airbed-host",
                    "container / container-ship / bop": "bop-host",
                    "liquefaction / liq": "lichost (expected, not piped yet)",
                },
            }
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def list_objects(
    component: str = "control",
    stream: str = "telemetry",
    host: str = "airbed-host",
    start: str = "",
    end: str = "",
    limit: int = 50,
) -> str:
    """List parquet object keys for a stream.

    Dates are inclusive (ISO-8601 or YYYY-MM-DD). Default range is the last 2 days UTC.
    """
    try:
        s3 = _s3()
        host_id = resolve_host(host, _walk_hosts(s3))
        loc = _find_location(s3, host_id, component, stream)
        if loc is None:
            return _json(
                {
                    "error": "no objects for that host/stream",
                    "host": host_id,
                    "component": component,
                    "stream": stream,
                    "buckets": BUCKETS,
                }
            )
        bucket, layout = loc["bucket"], loc["layout"]
        if start or end:
            now = now_utc()
            start_dt = parse_timestamp(start) if start else now - timedelta(days=2)
            end_dt = store.clamp_end(parse_timestamp(end)) if end else now
        else:
            start_dt, end_dt = _default_range(
                s3, component, stream, host_id, bucket, layout
            )
        keys = _keys_for_range(
            s3, component, stream, host_id, start_dt, end_dt, bucket, layout
        )
        cap = min(max(1, limit), 200)
        return _json(
            {
                "bucket": bucket,
                "layout": layout,
                "host": host_id,
                "component": component,
                "stream": stream,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "object_count": len(keys),
                "keys": keys[:cap],
            }
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def describe_schema(
    component: str = "control",
    stream: str = "telemetry",
    host: str = "airbed-host",
    day: str = "",
) -> str:
    """Describe columns of one parquet file for a stream (nested structs included).

    Telemetry files expose an `app` struct matching co3ntrol-rs JSON
    (app.system.airbed.sensors.*, app.system.bop.sensors.*, app.supervisor.lock, …).
    Console files have timestamp, level, message. Some early/broken files are
    kind=telemetry_unparsed with a raw message blob — skip those and pick another day
    if DESCRIBE looks empty.
    """
    try:
        s3 = _s3()
        host_id = resolve_host(host, _walk_hosts(s3))
        loc = _find_location(s3, host_id, component, stream)
        if loc is None:
            return _json(
                {
                    "error": "no objects for that host/stream",
                    "host": host_id,
                    "component": component,
                    "stream": stream,
                    "buckets": BUCKETS,
                }
            )
        bucket, layout = loc["bucket"], loc["layout"]
        if day:
            d = parse_timestamp(day)
            prefix = hive_prefix(component, stream, host_id, d.date(), layout=layout)
        else:
            prefix = hive_prefix(component, stream, host_id, layout=layout)
        items = _list_keys(s3, prefix, 40, bucket)
        if not items:
            return _json({"error": "no objects for that prefix", "prefix": prefix, "bucket": bucket})
        # Prefer a larger file — tiny ones are often unparsed stubs.
        items.sort(key=lambda o: o["bytes"], reverse=True)
        key = items[0]["key"]
        url = parquet_s3_url(bucket, key)
        cols, raw_rows = store.execute(f"DESCRIBE SELECT * FROM read_parquet('{url}')")
        rows = [dict(zip(cols, r)) for r in raw_rows]
        return _json(
            {
                "bucket": bucket,
                "layout": layout,
                "key": key,
                "bytes": items[0]["bytes"],
                "columns": rows,
            }
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def query_range(
    component: str = "control",
    stream: str = "telemetry",
    host: str = "airbed-host",
    start: str = "",
    end: str = "",
    select_sql: str = "",
    where_sql: str = "",
    limit: int = 50,
) -> str:
    """Query parquet for a host/stream over a time range (DuckDB).

    Default SELECT for telemetry:
      CAST(timestamp AS VARCHAR) AS ts, host-or-machine AS host, kind, app.supervisor.lock AS locked
    Default SELECT for console:
      CAST(timestamp AS VARCHAR) AS ts, level, message
    Pass select_sql to pull sensors, e.g.
      CAST(timestamp AS VARCHAR) AS ts, app.system.bop.sensors.co2_accumulator_dry_pressure AS accum_psi
      CAST(timestamp AS VARCHAR) AS ts, app.system.airbed.sensors.buffer_tank_pressure AS buffer_psi
    where_sql is a raw predicate without the WHERE keyword.
    Always CAST timestamp to VARCHAR (timestamp tz needs pytz which is not installed).
    Prefer projecting a few columns — SELECT * on telemetry structs is huge.
    """
    try:
        s3 = _s3()
        host_id = resolve_host(host, _walk_hosts(s3))
        loc = _find_location(s3, host_id, component, stream)
        if loc is None:
            return _json(
                {
                    "error": "no objects for that host/stream — call list_streams",
                    "host": host_id,
                    "component": component,
                    "stream": stream,
                    "buckets": BUCKETS,
                }
            )
        bucket, layout = loc["bucket"], loc["layout"]
        if start or end:
            now = now_utc()
            start_dt = parse_timestamp(start) if start else now - timedelta(hours=6)
            end_dt = store.clamp_end(parse_timestamp(end)) if end else now
        else:
            start_dt, end_dt = _default_range(
                s3, component, stream, host_id, bucket, layout
            )
        keys = _keys_for_range(
            s3, component, stream, host_id, start_dt, end_dt, bucket, layout
        )
        if not keys:
            return _json(
                {
                    "bucket": bucket,
                    "layout": layout,
                    "host": host_id,
                    "component": component,
                    "stream": stream,
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "row_count": 0,
                    "rows": [],
                    "note": "no parquet objects in range — call list_streams / list_objects",
                }
            )
        urls = [parquet_s3_url(bucket, k) for k in keys]
        from_sql = _read_parquet_sql(urls)
        if select_sql.strip():
            select_clause = select_sql.strip()
        elif stream == "console":
            select_clause = "CAST(timestamp AS VARCHAR) AS ts, level, message"
        elif layout == "machine":
            select_clause = (
                "CAST(timestamp AS VARCHAR) AS ts, machine AS host, kind, "
                "app.supervisor.lock AS locked"
            )
        else:
            select_clause = (
                "CAST(timestamp AS VARCHAR) AS ts, host, kind, "
                "app.supervisor.lock AS locked"
            )
        where_parts = [
            f"timestamp >= TIMESTAMPTZ '{start_dt.isoformat()}'",
            f"timestamp <= TIMESTAMPTZ '{end_dt.isoformat()}'",
        ]
        if stream == "telemetry" and "kind" not in (where_sql or "").lower():
            where_parts.append("(kind IS NULL OR kind = 'telemetry')")
        if where_sql.strip():
            where_parts.append(f"({where_sql.strip()})")
        sql = (
            f"SELECT {select_clause} FROM {from_sql} "
            f"WHERE {' AND '.join(where_parts)} "
            f"ORDER BY timestamp"
        )
        payload = _run_sql(sql, limit)
        payload.update(
            {
                "bucket": bucket,
                "layout": layout,
                "host": host_id,
                "component": component,
                "stream": stream,
                "objects": len(keys),
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
            }
        )
        return _json(payload)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def query_sql(sql: str, limit: int = 100) -> str:
    """Run a read-only DuckDB SELECT against the plant buckets.

    Parquet URLs:
      s3://provocative-dac-dev-beef/logs/control/telemetry/host=airbed-host/year=2026/month=08/day=30/*.parquet
      s3://provocative-dac-dev-a7f3/telemetry/machine=bop-host/year=2026/month=09/day=18/*.parquet
    Always pass union_by_name=true (schemas differ: parsed telemetry vs telemetry_unparsed).
    Example (container ship):
      SELECT CAST(timestamp AS VARCHAR) AS ts,
             app.system.bop.sensors.co2_accumulator_dry_pressure AS accum_psi,
             app.system.bop.sensors.product_supply_gas_out_flow AS slpm_out
      FROM read_parquet('s3://provocative-dac-dev-a7f3/telemetry/machine=bop-host/year=2026/month=09/day=18/*.parquet', union_by_name=true)
      ORDER BY timestamp DESC
      LIMIT 20
    Only SELECT/WITH/DESCRIBE/SHOW/EXPLAIN/FROM are allowed. S3 credentials are injected
    by this server — do not print them.
    """
    try:
        return _json(_run_sql(sql, limit))
    except Exception as exec_err:
        return _json({"error": str(exec_err)})


@mcp.tool()
def lookup_signal(query: str, system: str = "", limit: int = 8, deployed_only: bool = False) -> str:
    """Look up a plant signal or supervisor rule in the generated signal dictionary.

    Call this BEFORE writing SQL or PromQL and before naming a signal to an operator.
    Accepts a GUI label ("Vent/Bag Valve"), IO tag ("V17", "B90"), field name, Prom
    metric (co3ntrol_airbed_controls_vent_or_bag_valve), struct path, or loose words
    ("warm tank level"). Each match gives: sql (struct path for query_range), prom,
    gui_label/gui_section, tag, description/spec, units (+ how sure), read_path (which
    device/riser it is read from), wired, deployed_on (hosts whose deployed telemetry has
    it), and supervisor rules that read it. Rule names (extraction_bag_watchdog) return
    the rule's params, inputs, recovery actions and source.
    system: airbed | container | liquefaction | supervisor (optional filter).
    """
    try:
        return _json(sigs.lookup(query, system=system, limit=limit, deployed_only=deployed_only))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def find_transitions(
    host: str,
    signals: list[str],
    start: str = "",
    end: str = "",
    max_events: int = 60,
    gap_seconds: float = 10.0,
) -> str:
    """Exact edges for up to 8 signals over a time range (default: 6h before end).

    Each signal may be a dictionary name / GUI label / tag / app.… path, optionally with a
    comparison to find threshold crossings: "warm_tank_level < 14", "supervisor.lock == true".
    Booleans, strings and conditions report every value change with timestamps; plain floats
    report null↔value edges plus min/max/avg/first/last. Also reports data gaps longer than
    gap_seconds (missing samples ≠ a value change). Use this instead of eyeballing sampled rows.
    """
    try:
        return _json(investigate.find_transitions(host, signals, start, end, max_events, gap_seconds))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def why_locked(host: str, at: str = "", lookback_hours: float = 6.0) -> str:
    """Explain the most recent supervisor lock at/before `at` (default now) on a host.

    Finds the lock rising edge, the recovery that started, console watchdog/recovery/error
    lines around it (collapsed when repeating), fields that went null between the sample
    before and the edge (grouped by IO station — a device fault shows as many fields from one
    station going null), discrete changes, and evaluates each candidate watchdog's inputs at
    the edge including staleness (get_if_valid max_sample_age). Also reports restarts, when
    the lock cleared, and recent_changes: deploys and device I/O remaps that touched the
    affected signals/risers before the edge. Start every lock/watchdog investigation here.
    """
    try:
        return _json(investigate.why_locked(host, at, lookback_hours))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def recent_changes(host: str, at: str = "", signals: list[str] | None = None, hours: float = 72.0) -> str:
    """What changed before a fault on a host: code deploys, device I/O remaps, controller restarts.

    `at` is the fault onset (default now). `signals` are the affected fields (e.g. the ones that
    went null or misbehaved); remaps of those signals, or of other signals on the same backplane
    riser, are ranked first. Run this for every fault and judge each change on mechanism: a pin
    remap explains that output not actuating while its readback follows the command (match="direct"),
    not a station dropping out or Modbus exceptions.
    Reads the host change digest (hourly), so check digest_generated_at against the onset.
    """
    try:
        return _json(investigate.recent_changes(host, at, signals, hours))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def plant_snapshot(host: str, at: str = "", include_console: bool = True) -> str:
    """Compact state of one host at a time (default: latest archived sample).

    Supervisor lock/recovery/interlocks, uptime, controls that are ON and sensor values grouped
    by GUI section with units, wired fields that are null (grouped by IO station), and recent
    console warnings/errors. Archive lags up to ~1h; use Grafana Prom for live values.
    """
    try:
        return _json(investigate.plant_snapshot(host, at, include_console))
    except Exception as exc:
        return _json({"error": str(exc)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
