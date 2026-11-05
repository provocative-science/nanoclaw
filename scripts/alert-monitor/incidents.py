"""Group alert events into incidents so one fault produces one brief.

The first event for a subsystem opens an incident and gets a full brief. Later events
whose signatures (the monitor's per-edge keys, e.g. `container:watchdog:x`) were
already seen are only counted. A new signature produces an update, at most one per
`update_min_s`; signatures that arrive sooner wait for the next allowed update. An
incident closes after `quiet_s` without events.

State persists in $NANOCLAW_ROOT/data/alert-monitor/incidents.json so a monitor
restart does not re-page an open incident.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

INCIDENTS_REL_PATH = Path("data") / "alert-monitor" / "incidents.json"

Action = Literal["open", "update", "pending", "count"]


@dataclass
class Incident:
    id: str
    subsystem: str
    opened_at: float
    last_event_at: float
    signatures: list[str]
    events: int = 1
    briefs: int = 0
    last_brief_at: float = 0.0
    suppressed_by: str | None = None
    pending: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "opened_at": self.opened_at,
            "events": self.events,
            "briefs": self.briefs,
            "signatures": self.signatures,
        }


class IncidentBook:
    def __init__(self, nanoclaw_root: Path, *, quiet_s: float = 900.0, update_min_s: float = 300.0,
                 persist: bool = True) -> None:
        self.path = nanoclaw_root / INCIDENTS_REL_PATH
        self.quiet_s = quiet_s
        self.update_min_s = update_min_s
        self.persist = persist
        self.open: dict[str, Incident] = {}
        if persist:
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for sub, inc in (raw.get("open") or {}).items():
            try:
                self.open[sub] = Incident(**inc)
            except TypeError:
                continue

    def _save(self) -> None:
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"open": {k: asdict(v) for k, v in self.open.items()}}, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(self.path)

    def on_event(self, subsystem: str, signatures: list[str], now: float, *, alert_id: str,
                 suppressed_by: str | None = None) -> tuple[Action, Incident]:
        inc = self.open.get(subsystem)
        if inc is None or (inc.suppressed_by and not suppressed_by):
            inc = Incident(id=alert_id, subsystem=subsystem, opened_at=now, last_event_at=now,
                           signatures=list(dict.fromkeys(signatures)), suppressed_by=suppressed_by)
            self.open[subsystem] = inc
            self._save()
            return "open", inc
        inc.events += 1
        inc.last_event_at = now
        new = [s for s in signatures if s not in inc.signatures and s not in inc.pending]
        if not new:
            self._save()
            return "count", inc
        if suppressed_by or inc.suppressed_by:
            inc.signatures.extend(new)
            self._save()
            return "count", inc
        if now - inc.last_brief_at >= self.update_min_s:
            inc.signatures.extend(inc.pending + new)
            inc.pending = []
            self._save()
            return "update", inc
        inc.pending.extend(new)
        self._save()
        return "pending", inc

    def mark_brief(self, inc: Incident, now: float) -> None:
        inc.briefs += 1
        inc.last_brief_at = now
        self._save()

    def due_updates(self, now: float) -> list[Incident]:
        """Incidents with held-back signatures whose update interval has passed."""
        due = [i for i in self.open.values()
               if i.pending and not i.suppressed_by and now - i.last_brief_at >= self.update_min_s]
        for inc in due:
            inc.signatures.extend(inc.pending)
            inc.pending = []
        if due:
            self._save()
        return due

    def close_quiet(self, now: float) -> list[Incident]:
        closed = [i for i in self.open.values() if now - i.last_event_at >= self.quiet_s]
        for inc in closed:
            del self.open[inc.subsystem]
        if closed:
            self._save()
        return closed
