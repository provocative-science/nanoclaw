"""Liquefaction telemetry from Grafana Cloud Prometheus (Mimir) via the Grafana proxy.

bob49 cannot reach the liquefaction controller's HTTP API, but pirate-host's Alloy
scrapes co3ntrol-rs /metrics into Mimir. Gauges are named
co3ntrol_liquefaction_{controls|sensors}_<field>, which maps 1:1 onto the API's
liquefaction.controls / liquefaction.sensors objects, so the monitor's edge logic
runs unchanged on the reshaped sample.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

LIQ_SELECTOR = '{subsystem="liquefaction",__name__=~"co3ntrol_liquefaction_(controls|sensors)_.+"}'
LIQ_SYSTICK = 'co3ntrol_systick{subsystem="liquefaction"}'
_PREFIX = "co3ntrol_liquefaction_"


def _number(raw: str) -> int | float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return int(v) if v.is_integer() else v


def reshape_liquefaction(result: list[dict[str, Any]]) -> dict[str, Any]:
    """Prom instant-vector result → {"liquefaction": {"controls": {...}, "sensors": {...}}}."""
    groups: dict[str, dict[str, Any]] = {"controls": {}, "sensors": {}}
    for series in result:
        name = (series.get("metric") or {}).get("__name__", "")
        if not name.startswith(_PREFIX):
            continue
        group, _, key = name[len(_PREFIX):].partition("_")
        if group not in groups or not key:
            continue
        value = _number((series.get("value") or [None, None])[1])
        if value is not None:
            groups[group][key] = value
    return {"liquefaction": groups}


@dataclass
class LiqSample:
    sample: dict[str, Any] | None
    sample_ts: float | None
    systick: int | None


class PromClient:
    def __init__(self, *, grafana_url: str, grafana_token: str,
                 datasource_uid: str = "grafanacloud-prom", timeout_s: float = 10.0) -> None:
        self.grafana_url = grafana_url.rstrip("/")
        self.grafana_token = grafana_token
        self.datasource_uid = datasource_uid
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self.grafana_url and self.grafana_token)

    def query(self, promql: str) -> list[dict[str, Any]]:
        resp = requests.get(
            f"{self.grafana_url}/api/datasources/proxy/uid/{self.datasource_uid}/api/v1/query",
            params={"query": promql},
            headers={"Authorization": f"Bearer {self.grafana_token}"},
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prom query failed: {body.get('error') or body}")
        return body.get("data", {}).get("result", [])

    def fetch_liquefaction(self) -> LiqSample:
        """Latest liquefaction sample; sample_ts None when Mimir has nothing in its 5 min lookback."""
        tick = self.query(f"timestamp({LIQ_SYSTICK})")
        if not tick:
            return LiqSample(None, None, None)
        sample_ts = float(tick[0]["value"][1])
        systick_rows = self.query(LIQ_SYSTICK)
        systick = _number(systick_rows[0]["value"][1]) if systick_rows else None
        sample = reshape_liquefaction(self.query(LIQ_SELECTOR))
        sample["timestamp"] = sample_ts
        sample["systick"] = systick
        return LiqSample(sample, sample_ts, int(systick) if systick is not None else None)


def age_s(sample_ts: float | None, now: float | None = None) -> float | None:
    if sample_ts is None:
        return None
    return (time.time() if now is None else now) - sample_ts
