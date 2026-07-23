"""Query Loki for container automation abort lines (Ghost option B).

Independent of Grafana Alerting / OnCall — polls the Loki query API
(via Grafana datasource proxy or direct Loki basic auth) and detects
new matching log lines.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

# Same substrings as the Grafana LogQL plant rule.
ABORT_LINE_RE = re.compile(
    r"(failed!|Exiting for health reasons|Control set failed)",
    re.IGNORECASE,
)

DEFAULT_LOKI_QUERY = '{subsystem="container", service_name="co3ntrol-automation"}'


@dataclass(frozen=True)
class AbortLogHit:
    ts_ns: int
    line: str


def line_is_abort(line: str) -> bool:
    return bool(ABORT_LINE_RE.search(line))


def parse_loki_streams(payload: dict[str, Any]) -> list[AbortLogHit]:
    """Flatten Loki query_range result into chronological abort hits."""
    hits: list[AbortLogHit] = []
    result = (payload.get("data") or {}).get("result") or []
    for stream in result:
        for ts_s, line in stream.get("values") or []:
            try:
                ts_ns = int(ts_s)
            except (TypeError, ValueError):
                continue
            text = str(line)
            if line_is_abort(text):
                hits.append(AbortLogHit(ts_ns=ts_ns, line=text))
    hits.sort(key=lambda h: h.ts_ns)
    return hits


def new_hits_after(hits: list[AbortLogHit], after_ts_ns: int) -> list[AbortLogHit]:
    return [h for h in hits if h.ts_ns > after_ts_ns]


class LokiAbortClient:
    """Fetch recent automation abort lines from Loki."""

    def __init__(
        self,
        *,
        grafana_url: str = "",
        grafana_token: str = "",
        datasource_uid: str = "grafanacloud-logs",
        loki_url: str = "",
        loki_user: str = "",
        loki_token: str = "",
        query: str = DEFAULT_LOKI_QUERY,
        lookback_s: float = 120.0,
        timeout_s: float = 10.0,
    ) -> None:
        self.grafana_url = grafana_url.rstrip("/")
        self.grafana_token = grafana_token
        self.datasource_uid = datasource_uid
        self.loki_url = loki_url.rstrip("/")
        self.loki_user = loki_user
        self.loki_token = loki_token
        self.query = query
        self.lookback_s = lookback_s
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        if self.grafana_url and self.grafana_token:
            return True
        if self.loki_url and self.loki_user and self.loki_token:
            return True
        return False

    def _query_range_url(self, start_ns: int, end_ns: int) -> tuple[str, dict[str, str]]:
        params = urllib.parse.urlencode(
            {
                "query": self.query,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": "100",
                "direction": "forward",
            }
        )
        if self.grafana_url and self.grafana_token:
            url = (
                f"{self.grafana_url}/api/datasources/proxy/uid/"
                f"{self.datasource_uid}/loki/api/v1/query_range?{params}"
            )
            headers = {
                "Authorization": f"Bearer {self.grafana_token}",
                "Accept": "application/json",
            }
            return url, headers

        base = self.loki_url
        if base.endswith("/loki/api/v1/push"):
            base = base[: -len("/loki/api/v1/push")]
        url = f"{base}/loki/api/v1/query_range?{params}"
        token = base64.b64encode(f"{self.loki_user}:{self.loki_token}".encode()).decode()
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        }
        return url, headers

    def fetch_abort_hits(self, *, now: float | None = None) -> list[AbortLogHit]:
        if not self.enabled:
            return []
        now = time.time() if now is None else now
        end_ns = int(now * 1e9)
        start_ns = int((now - self.lookback_s) * 1e9)
        url, headers = self._query_range_url(start_ns, end_ns)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Loki HTTP {e.code}: {body}") from e
        except Exception as e:
            raise RuntimeError(f"Loki query failed: {e}") from e
        return parse_loki_streams(payload)
