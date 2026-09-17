#!/usr/bin/env python3
"""Stdio MCP server: query co3ntrol-rs parquet telemetry/logs in Wasabi S3."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
import duckdb
from mcp.server.fastmcp import FastMCP

from paths import (
    assert_read_only_sql,
    day_range,
    hive_prefix,
    parse_hive_key,
    parse_timestamp,
    parquet_s3_url,
    resolve_host,
    s3_endpoint_host,
)

# Following Qodo rule: No Hardcoded Credentials — keys come from env (secrets/mcp.env).
BUCKET = os.environ.get("TELEMETRY_S3_BUCKET", "provocative-dac-dev-beef")
REGION = os.environ.get("TELEMETRY_S3_REGION", "us-east-1")
ENDPOINT = os.environ.get(
    "TELEMETRY_S3_ENDPOINT", "https://s3.us-east-1.wasabisys.com"
)
ACCESS_KEY = os.environ.get("TELEMETRY_S3_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("TELEMETRY_S3_SECRET_KEY", "")

MAX_ROWS = int(os.environ.get("TELEMETRY_MAX_ROWS", "1000"))
HARD_MAX_ROWS = 5000
MAX_RESULT_CHARS = 400_000
MAX_KEYS_PER_RANGE = 400
LIST_STREAM_KEY_CAP = 8000

mcp = FastMCP("plant-telemetry")
_lock = threading.Lock()
_duck: duckdb.DuckDBPyConnection | None = None


def _require_creds() -> None:
    if not ACCESS_KEY or not SECRET_KEY:
        raise RuntimeError(
            "TELEMETRY_S3_ACCESS_KEY and TELEMETRY_S3_SECRET_KEY must be set"
        )


def _s3():
    _require_creds()
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )


def _configure_duck(con: duckdb.DuckDBPyConnection) -> None:
    _require_creds()
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    con.execute("SET s3_region=?", [REGION])
    con.execute("SET s3_endpoint=?", [s3_endpoint_host(ENDPOINT)])
    con.execute("SET s3_access_key_id=?", [ACCESS_KEY])
    con.execute("SET s3_secret_access_key=?", [SECRET_KEY])
    con.execute("SET s3_use_ssl=true")
    con.execute("SET s3_url_style='path'")


def _conn() -> duckdb.DuckDBPyConnection:
    global _duck
    if _duck is None:
        _duck = duckdb.connect(database=":memory:")
        _configure_duck(_duck)
    return _duck


def _json(payload: Any) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


def _prefixes(s3, prefix: str) -> list[str]:
    out: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix, Delimiter="/"):
        out.extend(p["Prefix"] for p in page.get("CommonPrefixes", []))
    return out


def _list_keys(s3, prefix: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            out.append(
                {
                    "key": obj["Key"],
                    "bytes": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )
            if len(out) >= limit:
                return out
    return out


def _keys_for_range(
    s3, component: str, stream: str, host: str, start: datetime, end: datetime
) -> list[str]:
    keys: list[str] = []
    for day in day_range(start, end):
        prefix = hive_prefix(component, stream, host, day)
        for item in _list_keys(s3, prefix, MAX_KEYS_PER_RANGE):
            keys.append(item["key"])
            if len(keys) >= MAX_KEYS_PER_RANGE:
                return keys
    return keys


def _prefix_int(prefix: str, label: str) -> int:
    seg = prefix.rstrip("/").rsplit("/", 1)[-1]
    if not seg.startswith(f"{label}="):
        return -1
    try:
        return int(seg.split("=", 1)[1])
    except ValueError:
        return -1


def _latest_partition_day(s3, component: str, stream: str, host: str) -> date | None:
    host_prefix = hive_prefix(component, stream, host)
    years = _prefixes(s3, host_prefix)
    if not years:
        return None
    year_p = max(years, key=lambda p: _prefix_int(p, "year"))
    months = _prefixes(s3, year_p)
    if not months:
        return None
    month_p = max(months, key=lambda p: _prefix_int(p, "month"))
    days = _prefixes(s3, month_p)
    if not days:
        return None
    day_p = max(days, key=lambda p: _prefix_int(p, "day"))
    y, m, d = _prefix_int(year_p, "year"), _prefix_int(month_p, "month"), _prefix_int(day_p, "day")
    if min(y, m, d) < 1:
        return None
    return date(y, m, d)


def _default_range(
    s3, component: str, stream: str, host: str
) -> tuple[datetime, datetime]:
    """Prefer the newest day that has a substantial parquet; else latest partition."""
    latest = _latest_partition_day(s3, component, stream, host)
    if latest is None:
        end = datetime.now(timezone.utc)
        return end - timedelta(days=2), end
    # Walk back up to 45 days for a file that looks like parsed telemetry (>=50 KiB).
    chosen = latest
    for offset in range(0, 46):
        day = latest - timedelta(days=offset)
        items = _list_keys(s3, hive_prefix(component, stream, host, day), 20)
        if any(int(i["bytes"]) >= 50_000 for i in items):
            chosen = day
            break
    start = datetime(chosen.year, chosen.month, chosen.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start, end


def _read_parquet_sql(urls: list[str]) -> str:
    quoted = ", ".join("'" + u.replace("'", "''") + "'" for u in urls)
    return f"read_parquet([{quoted}], union_by_name=true)"


def _run_sql(sql: str, max_rows: int) -> dict[str, Any]:
    safe = assert_read_only_sql(sql)
    cap = min(max(1, max_rows), HARD_MAX_ROWS)
    wrapped = f"SELECT * FROM ({safe}) AS plant_q LIMIT {cap}"
    with _lock:
        con = _conn()
        _configure_duck(con)
        rel = con.execute(wrapped)
        col_names = [d[0] for d in rel.description] if rel.description else []
        rows = rel.fetchall()
    records = [dict(zip(col_names, row)) for row in rows]
    text = _json({"columns": col_names, "row_count": len(records), "rows": records})
    truncated = False
    if len(text) > MAX_RESULT_CHARS:
        truncated = True
        # Shrink row payload until it fits.
        while records and len(text) > MAX_RESULT_CHARS:
            records = records[: max(1, len(records) // 2)]
            text = _json(
                {
                    "columns": col_names,
                    "row_count": len(records),
                    "truncated": True,
                    "rows": records,
                    "note": f"result truncated to {len(records)} rows / {MAX_RESULT_CHARS} chars",
                }
            )
    payload = json.loads(text)
    payload["truncated"] = truncated or bool(payload.get("truncated"))
    return payload


def _walk_hosts(s3) -> set[str]:
    hosts: set[str] = set()
    for component_prefix in _prefixes(s3, "logs/"):
        for stream_prefix in _prefixes(s3, component_prefix):
            for host_prefix in _prefixes(s3, stream_prefix):
                host_seg = host_prefix[len(stream_prefix) :].strip("/")
                host = host_seg.split("=", 1)[-1] if "=" in host_seg else host_seg
                if host:
                    hosts.add(host)
    return hosts


def _inventory(s3) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = []
    for component_prefix in _prefixes(s3, "logs/"):
        component = component_prefix[len("logs/") :].strip("/")
        for stream_prefix in _prefixes(s3, component_prefix):
            stream = stream_prefix[len(component_prefix) :].strip("/")
            for host_prefix in _prefixes(s3, stream_prefix):
                host_seg = host_prefix[len(stream_prefix) :].strip("/")
                host = host_seg.split("=", 1)[-1] if "=" in host_seg else host_seg
                keys = _list_keys(s3, host_prefix, LIST_STREAM_KEY_CAP)
                days: set[str] = set()
                bytes_total = 0
                for item in keys:
                    bytes_total += int(item["bytes"])
                    meta = parse_hive_key(item["key"])
                    if meta:
                        days.add(f"{meta['year']}-{meta['month']}-{meta['day']}")
                streams.append(
                    {
                        "component": component,
                        "stream": stream,
                        "host": host,
                        "objects": len(keys),
                        "capped": len(keys) >= LIST_STREAM_KEY_CAP,
                        "bytes": bytes_total,
                        "day_count": len(days),
                        "first_day": min(days) if days else None,
                        "last_day": max(days) if days else None,
                    }
                )
    return streams


@mcp.tool()
def list_streams() -> str:
    """List parquet streams in the plant telemetry bucket.

    Keys look like logs/{component}/{stream}/host={host}/year=YYYY/month=MM/day=DD/*.parquet.
    component is usually control or automation; stream is telemetry (JSON snapshots)
    or console (log lines). Call this first to see which hosts are present.
    Airbed is host=airbed-host today. Container-ship and liquefaction prefixes will
    appear when those nodes are piped in — do not assume they exist yet.
    """
    try:
        streams = _inventory(_s3())
        return _json(
            {
                "bucket": BUCKET,
                "endpoint": ENDPOINT,
                "streams": streams,
                "aliases": {
                    "airbed": "airbed-host",
                    "container / container-ship / bop": "bop-host (expected)",
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
        if start or end:
            now = datetime.now(timezone.utc)
            start_dt = parse_timestamp(start) if start else now - timedelta(days=2)
            end_dt = parse_timestamp(end) if end else now
        else:
            start_dt, end_dt = _default_range(s3, component, stream, host_id)
        keys = _keys_for_range(s3, component, stream, host_id, start_dt, end_dt)
        cap = min(max(1, limit), 200)
        return _json(
            {
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
    (app.system.airbed.sensors.*, app.supervisor.lock, …). Console files have
    timestamp, level, message. Some early/broken files are kind=telemetry_unparsed
    with a raw message blob — skip those and pick another day if DESCRIBE looks empty.
    """
    try:
        s3 = _s3()
        host_id = resolve_host(host, _walk_hosts(s3))
        if day:
            d = parse_timestamp(day)
            prefix = hive_prefix(component, stream, host_id, d.date())
        else:
            prefix = hive_prefix(component, stream, host_id)
        items = _list_keys(s3, prefix, 40)
        if not items:
            return _json({"error": "no objects for that prefix", "prefix": prefix})
        # Prefer a larger file — tiny ones are often unparsed stubs.
        items.sort(key=lambda o: o["bytes"], reverse=True)
        key = items[0]["key"]
        url = parquet_s3_url(BUCKET, key)
        with _lock:
            con = _conn()
            _configure_duck(con)
            rel = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{url}')")
            cols = [d[0] for d in rel.description] if rel.description else []
            rows = [dict(zip(cols, r)) for r in rel.fetchall()]
        return _json({"key": key, "bytes": items[0]["bytes"], "columns": rows})
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
      CAST(timestamp AS VARCHAR) AS ts, host, kind, app.supervisor.lock AS locked
    Default SELECT for console:
      CAST(timestamp AS VARCHAR) AS ts, level, message
    Pass select_sql to pull sensors, e.g.
      CAST(timestamp AS VARCHAR) AS ts, app.system.airbed.sensors.buffer_tank_pressure AS buffer_psi
    where_sql is a raw predicate without the WHERE keyword.
    Always CAST timestamp to VARCHAR (timestamp tz needs pytz which is not installed).
    Prefer projecting a few columns — SELECT * on telemetry structs is huge.
    """
    try:
        s3 = _s3()
        host_id = resolve_host(host, _walk_hosts(s3))
        if start or end:
            now = datetime.now(timezone.utc)
            start_dt = parse_timestamp(start) if start else now - timedelta(hours=6)
            end_dt = parse_timestamp(end) if end else now
        else:
            start_dt, end_dt = _default_range(s3, component, stream, host_id)
        keys = _keys_for_range(s3, component, stream, host_id, start_dt, end_dt)
        if not keys:
            return _json(
                {
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
        urls = [parquet_s3_url(BUCKET, k) for k in keys]
        from_sql = _read_parquet_sql(urls)
        if select_sql.strip():
            select_clause = select_sql.strip()
        elif stream == "console":
            select_clause = "CAST(timestamp AS VARCHAR) AS ts, level, message"
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
    """Run a read-only DuckDB SELECT against the plant bucket.

    Parquet URLs:
      s3://provocative-dac-dev-beef/logs/control/telemetry/host=airbed-host/year=2026/month=08/day=30/*.parquet
    Always pass union_by_name=true (schemas differ: parsed telemetry vs telemetry_unparsed).
    Example:
      SELECT CAST(timestamp AS VARCHAR) AS ts,
             app.system.airbed.sensors.buffer_tank_pressure AS buffer_psi
      FROM read_parquet('s3://provocative-dac-dev-beef/logs/control/telemetry/host=airbed-host/year=2026/month=08/day=30/*.parquet', union_by_name=true)
      ORDER BY timestamp DESC
      LIMIT 20
    Only SELECT/WITH/DESCRIBE/SHOW/EXPLAIN/FROM are allowed. S3 credentials are injected
    by this server — do not print them.
    """
    try:
        return _json(_run_sql(sql, limit))
    except Exception as exec_err:
        return _json({"error": str(exec_err)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
