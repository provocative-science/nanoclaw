"""S3 + DuckDB access to co3ntrol-rs parquet, shared by the MCP server and host jobs.

Importable without the `mcp` package so host scripts (signal dictionary,
knowledge-store status checks) reuse the same bucket/layout logic.
"""

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

from paths import (
    assert_read_only_sql,
    configured_buckets,
    day_range,
    hive_prefix,
    parse_hive_key,
    parse_timestamp,
    parquet_s3_url,
    resolve_host,
    s3_endpoint_host,
    strip_outbound_proxy,
)

# Must run before DuckDB httpfs / boto3 clients are created.
strip_outbound_proxy()

# Following Qodo rule: No Hardcoded Credentials — keys come from env (secrets/mcp.env).
BUCKETS = configured_buckets(
    os.environ.get("TELEMETRY_S3_BUCKET", "provocative-dac-dev-beef"),
    os.environ.get("TELEMETRY_S3_BUCKETS", ""),
)
BUCKET = BUCKETS[0]
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

_lock = threading.Lock()
_duck: duckdb.DuckDBPyConnection | None = None


def time_cutoff() -> datetime | None:
    """Replay/eval clamp: no data after this instant is visible (TELEMETRY_TIME_CUTOFF)."""
    raw = os.environ.get("TELEMETRY_TIME_CUTOFF", "").strip()
    if not raw:
        return None
    return parse_timestamp(raw)


def clamp_end(end: datetime) -> datetime:
    cutoff = time_cutoff()
    if cutoff is not None and end > cutoff:
        return cutoff
    return end


def now_utc() -> datetime:
    """Wall clock, or the eval cutoff when replaying a past incident."""
    return clamp_end(datetime.now(timezone.utc))


def require_creds() -> None:
    if not ACCESS_KEY or not SECRET_KEY:
        raise RuntimeError(
            "TELEMETRY_S3_ACCESS_KEY and TELEMETRY_S3_SECRET_KEY must be set"
        )


def s3_client():
    require_creds()
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )


def configure_duck(con: duckdb.DuckDBPyConnection) -> None:
    require_creds()
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    con.execute("SET s3_region=?", [REGION])
    con.execute("SET s3_endpoint=?", [s3_endpoint_host(ENDPOINT)])
    con.execute("SET s3_access_key_id=?", [ACCESS_KEY])
    con.execute("SET s3_secret_access_key=?", [SECRET_KEY])
    con.execute("SET s3_use_ssl=true")
    con.execute("SET s3_url_style='path'")


def duck() -> duckdb.DuckDBPyConnection:
    global _duck
    if _duck is None:
        _duck = duckdb.connect(database=":memory:")
        configure_duck(_duck)
    return _duck


def to_json(payload: Any) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


def prefixes(s3, prefix: str, bucket: str) -> list[str]:
    out: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        out.extend(p["Prefix"] for p in page.get("CommonPrefixes", []))
    return out


def list_keys(s3, prefix: str, limit: int, bucket: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
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


def keys_for_range(
    s3,
    component: str,
    stream: str,
    host: str,
    start: datetime,
    end: datetime,
    bucket: str,
    layout: str,
) -> list[str]:
    end = clamp_end(end)
    keys: list[str] = []
    for day in day_range(start, end):
        prefix = hive_prefix(component, stream, host, day, layout=layout)
        for item in list_keys(s3, prefix, MAX_KEYS_PER_RANGE, bucket):
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


def latest_partition_day(
    s3, component: str, stream: str, host: str, bucket: str, layout: str
) -> date | None:
    host_prefix = hive_prefix(component, stream, host, layout=layout)
    years = prefixes(s3, host_prefix, bucket)
    if not years:
        return None
    year_p = max(years, key=lambda p: _prefix_int(p, "year"))
    months = prefixes(s3, year_p, bucket)
    if not months:
        return None
    month_p = max(months, key=lambda p: _prefix_int(p, "month"))
    days = prefixes(s3, month_p, bucket)
    if not days:
        return None
    day_p = max(days, key=lambda p: _prefix_int(p, "day"))
    y, m, d = _prefix_int(year_p, "year"), _prefix_int(month_p, "month"), _prefix_int(day_p, "day")
    if min(y, m, d) < 1:
        return None
    latest = date(y, m, d)
    cutoff = time_cutoff()
    if cutoff is not None and latest > cutoff.date():
        return cutoff.date()
    return latest


def default_range(
    s3, component: str, stream: str, host: str, bucket: str, layout: str
) -> tuple[datetime, datetime]:
    """Prefer the newest day that has a substantial parquet; else latest partition."""
    latest = latest_partition_day(s3, component, stream, host, bucket, layout)
    if latest is None:
        end = now_utc()
        return end - timedelta(days=2), end
    # Walk back up to 45 days for a file that looks like parsed telemetry (>=50 KiB).
    chosen = latest
    for offset in range(0, 46):
        day = latest - timedelta(days=offset)
        items = list_keys(
            s3, hive_prefix(component, stream, host, day, layout=layout), 20, bucket
        )
        if any(int(i["bytes"]) >= 50_000 for i in items):
            chosen = day
            break
    start = datetime(chosen.year, chosen.month, chosen.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start, clamp_end(end)


def stream_exists(
    s3, bucket: str, layout: str, component: str, stream: str, host: str
) -> bool:
    prefix = hive_prefix(component, stream, host, layout=layout)
    if prefixes(s3, prefix, bucket):
        return True
    return bool(list_keys(s3, prefix, 1, bucket))


def find_location(
    s3, host: str, component: str, stream: str
) -> dict[str, str] | None:
    for bucket in BUCKETS:
        for layout in ("nested", "machine"):
            if stream_exists(s3, bucket, layout, component, stream, host):
                return {"bucket": bucket, "layout": layout}
    return None


def summarize_prefix(s3, bucket: str, host_prefix: str) -> dict[str, Any]:
    keys = list_keys(s3, host_prefix, LIST_STREAM_KEY_CAP, bucket)
    days: set[str] = set()
    bytes_total = 0
    for item in keys:
        bytes_total += int(item["bytes"])
        meta = parse_hive_key(item["key"])
        if meta:
            days.add(f"{meta['year']}-{meta['month']}-{meta['day']}")
    return {
        "objects": len(keys),
        "capped": len(keys) >= LIST_STREAM_KEY_CAP,
        "bytes": bytes_total,
        "day_count": len(days),
        "first_day": min(days) if days else None,
        "last_day": max(days) if days else None,
    }


def read_parquet_sql(urls: list[str]) -> str:
    quoted = ", ".join("'" + u.replace("'", "''") + "'" for u in urls)
    return f"read_parquet([{quoted}], union_by_name=true)"


def execute(sql: str, params: list[Any] | None = None) -> tuple[list[str], list[tuple]]:
    """Run trusted, server-built SQL (no read-only check) and return columns + rows."""
    with _lock:
        con = duck()
        configure_duck(con)
        rel = con.execute(sql, params or [])
        cols = [d[0] for d in rel.description] if rel.description else []
        rows = rel.fetchall()
    return cols, rows


def _after_cutoff(record: dict[str, Any], cutoff: datetime) -> bool:
    for key in ("ts", "timestamp", "time"):
        val = record.get(key)
        if val is None:
            continue
        try:
            when = val if isinstance(val, datetime) else parse_timestamp(str(val))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when > cutoff
    return False


def run_sql(sql: str, max_rows: int) -> dict[str, Any]:
    safe = assert_read_only_sql(sql)
    cap = min(max(1, max_rows), HARD_MAX_ROWS)
    wrapped = f"SELECT * FROM ({safe}) AS plant_q LIMIT {cap}"
    col_names, rows = execute(wrapped)
    records = [dict(zip(col_names, row)) for row in rows]
    cutoff = time_cutoff()
    hidden = 0
    if cutoff is not None:
        kept = [r for r in records if not _after_cutoff(r, cutoff)]
        hidden = len(records) - len(kept)
        records = kept
    text = to_json({"columns": col_names, "row_count": len(records), "rows": records})
    truncated = False
    if len(text) > MAX_RESULT_CHARS:
        truncated = True
        # Shrink row payload until it fits.
        while records and len(text) > MAX_RESULT_CHARS:
            records = records[: max(1, len(records) // 2)]
            text = to_json(
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
    if hidden:
        payload["rows_hidden_after_cutoff"] = hidden
    return payload


def inventory(s3) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = []
    for bucket in BUCKETS:
        for component_prefix in prefixes(s3, "logs/", bucket):
            component = component_prefix[len("logs/") :].strip("/")
            if component.startswith("machine="):
                continue
            for stream_prefix in prefixes(s3, component_prefix, bucket):
                stream = stream_prefix[len(component_prefix) :].strip("/")
                for host_prefix in prefixes(s3, stream_prefix, bucket):
                    host_seg = host_prefix[len(stream_prefix) :].strip("/")
                    host = host_seg.split("=", 1)[-1] if "=" in host_seg else host_seg
                    if not host:
                        continue
                    streams.append(
                        {
                            "bucket": bucket,
                            "layout": "nested",
                            "component": component,
                            "stream": stream,
                            "host": host,
                            **summarize_prefix(s3, bucket, host_prefix),
                        }
                    )
        for root, stream in (("telemetry/", "telemetry"), ("logs/", "console")):
            for host_prefix in prefixes(s3, root, bucket):
                host_seg = host_prefix[len(root) :].strip("/")
                if not host_seg.startswith("machine="):
                    continue
                host = host_seg.split("=", 1)[-1]
                if not host:
                    continue
                streams.append(
                    {
                        "bucket": bucket,
                        "layout": "machine",
                        "component": "control",
                        "stream": stream,
                        "host": host,
                        **summarize_prefix(s3, bucket, host_prefix),
                    }
                )
    return streams


_host_cache: set[str] | None = None


def known_hosts(s3) -> set[str]:
    """Hosts that have at least one stream (cached per process; inventory is slow)."""
    global _host_cache
    if _host_cache is None:
        hosts: set[str] = set()
        for bucket in BUCKETS:
            for component_prefix in prefixes(s3, "logs/", bucket):
                if component_prefix[len("logs/") :].startswith("machine="):
                    hosts.add(component_prefix[len("logs/") :].strip("/").split("=", 1)[1])
                    continue
                for stream_prefix in prefixes(s3, component_prefix, bucket):
                    for host_prefix in prefixes(s3, stream_prefix, bucket):
                        seg = host_prefix[len(stream_prefix) :].strip("/")
                        hosts.add(seg.split("=", 1)[-1])
            for host_prefix in prefixes(s3, "telemetry/", bucket):
                seg = host_prefix[len("telemetry/") :].strip("/")
                if seg.startswith("machine="):
                    hosts.add(seg.split("=", 1)[1])
        _host_cache = {h for h in hosts if h}
    return _host_cache


class Source:
    """A resolved host/stream location plus a FROM clause over a time range."""

    def __init__(self, host: str, component: str, stream: str, bucket: str, layout: str):
        self.host = host
        self.component = component
        self.stream = stream
        self.bucket = bucket
        self.layout = layout

    @property
    def host_column(self) -> str:
        return "machine" if self.layout == "machine" else "host"

    def from_sql(self, s3, start: datetime, end: datetime) -> str | None:
        keys = keys_for_range(
            s3, self.component, self.stream, self.host, start, end, self.bucket, self.layout
        )
        if not keys:
            return None
        return read_parquet_sql([parquet_s3_url(self.bucket, k) for k in keys])

    def time_where(self, start: datetime, end: datetime) -> str:
        end = clamp_end(end)
        parts = [
            f"timestamp >= TIMESTAMPTZ '{start.isoformat()}'",
            f"timestamp <= TIMESTAMPTZ '{end.isoformat()}'",
        ]
        if self.stream == "telemetry":
            parts.append("(kind IS NULL OR kind = 'telemetry')")
        return " AND ".join(parts)


def resolve_source(s3, host: str, component: str = "control", stream: str = "telemetry") -> Source:
    host_id = resolve_host(host, known_hosts(s3))
    loc = find_location(s3, host_id, component, stream)
    if loc is None:
        raise LookupError(
            f"no parquet for host={host_id} component={component} stream={stream} "
            f"in buckets {BUCKETS} — call list_streams (liquefaction is Prom-only)"
        )
    return Source(host_id, component, stream, loc["bucket"], loc["layout"])
