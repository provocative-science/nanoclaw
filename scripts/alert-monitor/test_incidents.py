"""Offline tests for plant modes, incident grouping, Prom liquefaction and HTTP backoff."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import requests

import monitor as monitor_mod
import plant_modes
from incidents import IncidentBook
from monitor import AlertMonitor
from prom_source import LiqSample, reshape_liquefaction


class Capture(AlertMonitor):
    """Real fire()/incident logic; captures dispatches and plain lines."""

    def __init__(self, root: Path, prom=None, **kw) -> None:
        super().__init__(
            liq_base="http://liq.test", bop_base="http://bop.test", liq_token="", bop_token="",
            nanoclaw_root=root, ipc_group_folder="telegram_main", target_jid="tg:test",
            cooldown_s=300, poll_interval_s=1, dry_run=False, prom_client=prom,
            persist_incidents=False, **kw,
        )
        self.dispatched: list[tuple[str, bool, list[str]]] = []
        self.lines: list[str] = []
        self.archived: list[str] = []

    def _dispatch(self, alert, inc, *, update):  # type: ignore[override]
        self.dispatched.append((alert["kind"], update, list(alert["incident"]["new_signatures"])))
        self.incidents.mark_brief(inc, time.time())

    def _send_line(self, text, chat_jid=None):  # type: ignore[override]
        self.lines.append(text)

    def _archive(self, alert):  # type: ignore[override]
        self.archived.append(alert["kind"])


def watchdog(name: str) -> dict:
    return {"subsystem": "container", "kind": "watchdog_trigger", "watchdog": name}


def set_mode(root: Path, sub: str, mode: str, until: float | None = None, notify: str = "none") -> None:
    data = plant_modes.load(root)
    data["subsystems"][sub] = {
        "mode": mode, "reason": "electrical work", "set_by": "Spark", "set_at": plant_modes.iso(time.time()),
        "until": plant_modes.iso(until) if until else None, "notify": notify, "chat_jid": None,
    }
    plant_modes.save(root, data)


def test_plant_modes_expiry(root: Path) -> None:
    now = time.time()
    set_mode(root, "container", "maintenance", until=now - 1)
    set_mode(root, "liquefaction", "offline")
    data = plant_modes.load(root)
    data["muted_bits"] = [{"subsystem": "liquefaction", "severity": "warning", "bit": 2, "until": None}]
    plant_modes.save(root, data)
    assert plant_modes.active_mode(root, "container", now) is None
    assert plant_modes.active_mode(root, "liquefaction", now)["mode"] == "offline"
    assert plant_modes.muted_bits(root, "liquefaction", "warning", now) == {2}
    removed = plant_modes.expire(root, now)
    assert [r["subsystem"] for r in removed] == ["container"]
    assert "container" not in plant_modes.load(root)["subsystems"]


def test_incident_book() -> None:
    book = IncidentBook(Path("/nonexistent"), quiet_s=900, update_min_s=300, persist=False)
    t = 1000.0
    action, inc = book.on_event("container", ["a"], t, alert_id="i1")
    assert action == "open"
    book.mark_brief(inc, t)
    assert book.on_event("container", ["a"], t + 60, alert_id="x")[0] == "count"
    assert book.on_event("container", ["b"], t + 120, alert_id="x")[0] == "pending"
    assert book.due_updates(t + 200) == []
    due = book.due_updates(t + 301)
    assert [i.id for i in due] == ["i1"] and "b" in inc.signatures
    book.mark_brief(inc, t + 301)
    assert book.on_event("container", ["c"], t + 700, alert_id="x")[0] == "update"
    assert book.close_quiet(t + 700 + 899) == []
    closed = book.close_quiet(t + 700 + 900)
    assert [i.events for i in closed] == [4]
    assert book.on_event("liquefaction", ["q"], t, alert_id="s1", suppressed_by="offline")[0] == "open"
    assert book.on_event("liquefaction", ["r"], t + 1, alert_id="s2")[0] == "open"


def test_one_brief_per_incident(root: Path) -> None:
    m = Capture(root)
    for _ in range(4):
        m.fire(watchdog("extraction_bag_watchdog"), ["container:watchdog:extraction_bag_watchdog"])
    assert m.dispatched == [("watchdog_trigger", False, ["container:watchdog:extraction_bag_watchdog"])]
    assert m.archived == ["watchdog_trigger"]
    inc = m.incidents.open["container"]
    inc.last_brief_at -= 301
    m.fire(watchdog("hot_loop_temp_watchdog"), ["container:watchdog:hot_loop_temp_watchdog"])
    assert m.dispatched[-1] == ("watchdog_trigger", True, ["container:watchdog:hot_loop_temp_watchdog"])
    m.fire(watchdog("warm_loop_temp_watchdog"), ["container:watchdog:warm_loop_temp_watchdog"])
    assert len(m.dispatched) == 2
    inc.last_brief_at -= 301
    m.tick_incidents()
    assert m.dispatched[-1] == ("watchdog_trigger", True, ["container:watchdog:warm_loop_temp_watchdog"])
    m.tick_incidents(time.time() + 901)
    assert len(m.lines) == 1 and "closing the incident" in m.lines[0] and "6 events" in m.lines[0]


def test_mode_suppresses_agent_run(root: Path) -> None:
    set_mode(root, "container", "offline", notify="line")
    m = Capture(root)
    m.fire(watchdog("extraction_bag_watchdog"), ["container:watchdog:extraction_bag_watchdog"])
    m.fire(watchdog("hot_loop_temp_watchdog"), ["container:watchdog:hot_loop_temp_watchdog"])
    assert m.dispatched == []
    assert len(m.lines) == 1 and "container is offline" in m.lines[0]
    m.tick_incidents(time.time() + 901)
    assert len(m.lines) == 1
    m.fire({"subsystem": "liquefaction", "kind": "tower_error"}, ["liquefaction:tower_error"])
    assert [d[0] for d in m.dispatched] == ["tower_error"]


def test_mode_window_end_announced(root: Path) -> None:
    set_mode(root, "container", "maintenance", until=time.time() + 60)
    m = Capture(root)
    m.tick_incidents()
    assert m.lines == []
    m.tick_incidents(time.time() + 61)
    assert len(m.lines) == 1 and "maintenance window ended" in m.lines[0]


def liq_sample(warn_mask: int = 0, tower_warn: int = 0) -> dict:
    return {"liquefaction": {"controls": {"tower_warning_indicator": tower_warn, "tower_error_indicator": 0},
                             "sensors": {"system_state_code": 1, "active_warnings": warn_mask, "active_errors": 0}}}


def test_muted_bits_suppress_tower(root: Path) -> None:
    data = plant_modes.load(root)
    data["muted_bits"] = [{"subsystem": "liquefaction", "severity": "warning", "bit": 2, "until": None}]
    plant_modes.save(root, data)
    m = Capture(root)
    m.process_liquefaction(liq_sample())
    m.process_liquefaction(liq_sample(warn_mask=1 << 2, tower_warn=1))
    assert m.dispatched == []
    m.process_liquefaction(liq_sample(warn_mask=(1 << 2) | (1 << 3), tower_warn=1))
    assert m.dispatched == [("new_warning_bits", False, ["liquefaction:warn:bit3"])]


class FakeProm:
    enabled = True

    def __init__(self) -> None:
        self.next: LiqSample = LiqSample(None, None, None)

    def fetch_liquefaction(self) -> LiqSample:
        return self.next


def test_prom_stopped_and_resumed(root: Path) -> None:
    prom = FakeProm()
    m = Capture(root, prom=prom, liq_stale_s=180)
    now = time.time()
    m.poll_liquefaction_prom(now)
    assert m.dispatched == [] and m.state.liq_stale
    prom.next = LiqSample({**liq_sample(), "timestamp": now}, now, 5000)
    m.poll_liquefaction_prom(now + 15)
    assert m.state.liq_primed and not m.state.liq_stale and m.lines == []
    prom.next = LiqSample({**liq_sample(), "timestamp": now}, now, 5000)
    m.poll_liquefaction_prom(now + 200)
    assert [d[0] for d in m.dispatched] == ["stopped_reporting"]
    later = now + 1500
    prom.next = LiqSample({**liq_sample(tower_warn=1, warn_mask=8), "timestamp": later}, later, 3)
    m.poll_liquefaction_prom(later + 5)
    assert len(m.lines) == 1 and "reporting again after 25 min" in m.lines[0]
    assert len(m.dispatched) == 1, "first sample after a gap only re-primes"
    rd = m.snapshot_recent_deploy()
    assert rd["checks"][0] == {**rd["checks"][0], "subsystem": "liquefaction", "systick": 3, "recent": True}
    assert rd["likely"]


def test_reshape_liquefaction() -> None:
    out = reshape_liquefaction([
        {"metric": {"__name__": "co3ntrol_liquefaction_controls_tower_error_indicator"}, "value": [1, "1"]},
        {"metric": {"__name__": "co3ntrol_liquefaction_sensors_o2_ppm"}, "value": [1, "12.5"]},
        {"metric": {"__name__": "co3ntrol_liquefaction_sensors_active_warnings"}, "value": [1, "NaN"]},
        {"metric": {"__name__": "co3ntrol_systick"}, "value": [1, "9"]},
    ])
    assert out == {"liquefaction": {"controls": {"tower_error_indicator": 1}, "sensors": {"o2_ppm": 12.5}}}


def test_http_backoff() -> None:
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.exceptions.ConnectionError("down")

    orig = monitor_mod.requests.get
    monitor_mod.requests.get = boom
    try:
        assert monitor_mod.fetch_latest("http://backoff.test/x", "t", "") is None
        assert monitor_mod.fetch_latest("http://backoff.test/x", "t", "") is None
    finally:
        monitor_mod.requests.get = orig
        monitor_mod._unreachable.pop("http://backoff.test/x", None)
    assert len(calls) == 1


if __name__ == "__main__":
    for fn in (test_plant_modes_expiry, test_one_brief_per_incident, test_mode_suppresses_agent_run,
               test_mode_window_end_announced, test_muted_bits_suppress_tower, test_prom_stopped_and_resumed):
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
    test_incident_book()
    test_reshape_liquefaction()
    test_http_backoff()
    print(json.dumps({"ok": True}))
