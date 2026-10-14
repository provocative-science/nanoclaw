"""Pure helpers for the plant telemetry S3 layout (no AWS/DuckDB imports)."""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, MutableMapping

# Hive key (airbed / beef): logs/{component}/{stream}/host={host}/year=/month=/day=/
KEY_RE = re.compile(
    r"^logs/(?P<component>[^/]+)/(?P<stream>[^/]+)/host=(?P<host>[^/]+)"
    r"/year=(?P<year>\d+)/month=(?P<month>\d+)/day=(?P<day>\d+)/"
)
# Hive key (container-ship / a7f3): {telemetry|logs}/machine={host}/year=/month=/day=/
KEY_RE_MACHINE = re.compile(
    r"^(?P<root>telemetry|logs)/machine=(?P<host>[^/]+)"
    r"/year=(?P<year>\d+)/month=(?P<month>\d+)/day=(?P<day>\d+)/"
)

# Friendly names used in Grafana / chat → S3 hive host= segment.
# list_streams is authoritative; aliases only apply when the alias is not already a host.
HOST_ALIASES = {
    "airbed": "airbed-host",
    "airbed-host": "airbed-host",
    "pirate-host": "airbed-host",
    "container": "bop-host",
    "container-ship": "bop-host",
    "containership": "bop-host",
    "bop": "bop-host",
    "bop-host": "bop-host",
    "liquefaction": "lichost",
    "liq": "lichost",
    "lichost": "lichost",
}

_LEADING_OK = {
    "SELECT",
    "WITH",
    "DESCRIBE",
    "DESC",
    "SUMMARIZE",
    "SHOW",
    "EXPLAIN",
    "FROM",
    "PIVOT",
    "UNPIVOT",
}

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def resolve_host(name: str, known_hosts: Iterable[str] | None = None) -> str:
    raw = (name or "").strip()
    if not raw:
        raise ValueError("host is required")
    known = {h for h in (known_hosts or []) if h}
    if raw in known:
        return raw
    aliased = HOST_ALIASES.get(raw.lower(), raw)
    if known and aliased not in known and raw not in known:
        # Keep the alias (expected future host) but prefer an exact known match on lowercase.
        lower = {h.lower(): h for h in known}
        if raw.lower() in lower:
            return lower[raw.lower()]
        if aliased.lower() in lower:
            return lower[aliased.lower()]
    return aliased


def parse_hive_key(key: str) -> dict[str, str] | None:
    match = KEY_RE.match(key)
    if match:
        meta = match.groupdict()
        meta["layout"] = "nested"
        return meta
    match = KEY_RE_MACHINE.match(key)
    if not match:
        return None
    meta = match.groupdict()
    root = meta.pop("root")
    meta["layout"] = "machine"
    meta["component"] = "control"
    meta["stream"] = "telemetry" if root == "telemetry" else "console"
    return meta


def configured_buckets(primary: str | None, extra: str | None = None) -> list[str]:
    """TELEMETRY_S3_BUCKETS (comma-separated) wins; else TELEMETRY_S3_BUCKET."""
    extra_list = [b.strip() for b in (extra or "").split(",") if b.strip()]
    if extra_list:
        # preserve order, drop dupes
        return list(dict.fromkeys(extra_list))
    name = (primary or "").strip() or "provocative-dac-dev-beef"
    return [name]


def hive_prefix(
    component: str,
    stream: str,
    host: str,
    day: date | None = None,
    layout: str = "nested",
) -> str:
    if layout == "machine":
        root = "telemetry" if stream == "telemetry" else "logs"
        base = f"{root}/machine={host}/"
    else:
        base = f"logs/{component}/{stream}/host={host}/"
    if day is None:
        return base
    return f"{base}year={day.year:04d}/month={day.month:02d}/day={day.day:02d}/"


def parse_timestamp(value: str) -> datetime:
    """Parse ISO-8601, date-only, or unix seconds into an aware UTC datetime."""
    text = (value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if re.fullmatch(r"\d{10,13}", text):
        n = int(text)
        if n > 10_000_000_000:  # ms
            n = n / 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unrecognized timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_range(start: datetime, end: datetime) -> list[date]:
    if end < start:
        raise ValueError("end must be on or after start")
    days: list[date] = []
    cur = start.date()
    last = end.date()
    while cur <= last:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def parquet_s3_url(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def glob_s3_url(
    bucket: str,
    component: str,
    stream: str,
    host: str,
    day: date,
    layout: str = "nested",
) -> str:
    return (
        f"s3://{bucket}/{hive_prefix(component, stream, host, day, layout=layout)}*.parquet"
    )


def strip_sql_comments(sql: str) -> str:
    no_block = _COMMENT_RE.sub(" ", sql)
    return _LINE_COMMENT_RE.sub(" ", no_block)


def assert_read_only_sql(sql: str) -> str:
    """Return the stripped SQL (single statement) or raise ValueError."""
    body = strip_sql_comments(sql).strip()
    if not body:
        raise ValueError("empty SQL")
    if body.endswith(";"):
        body = body[:-1].strip()
    if ";" in body:
        raise ValueError("multiple SQL statements are not allowed")
    token = body.lstrip("(").split(None, 1)[0].upper() if body else ""
    if token not in _LEADING_OK:
        raise ValueError(f"only read queries are allowed (got {token or 'empty'})")
    return body


def s3_endpoint_host(endpoint: str) -> str:
    """DuckDB wants host[:port] with no scheme."""
    text = (endpoint or "").strip()
    text = re.sub(r"^https?://", "", text, flags=re.I)
    return text.rstrip("/")


# OneCLI injects HTTP(S)_PROXY with userinfo (http://x:token@host:port). DuckDB
# httpfs cannot parse that form ("Failed to parse http_proxy into a host and
# port"), so parquet reads fail while boto3 listing still works. Wasabi is
# reachable directly from agent containers, same as Notion MCP.
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def strip_outbound_proxy(
    env: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Remove proxy env vars so DuckDB talks to Wasabi directly. Returns removed keys."""
    target = os.environ if env is None else env
    removed: list[str] = []
    for key in PROXY_ENV_KEYS:
        if key in target:
            target.pop(key, None)
            removed.append(key)
    return removed
