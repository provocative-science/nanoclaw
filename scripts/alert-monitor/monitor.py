#!/usr/bin/env python3
"""
Plant alert monitor for NanoClaw (runs on bob49 — not the Pi LED client).

Polls liquefaction tower indicators / HAZOP bitmasks / system_state ERROR
transitions (from Grafana Cloud Prometheus by default; the controller API when
LIQ_SOURCE=api), container supervisor lock + triggered_interlocks, and (option B)
Loki abort / watchdog lines. Rising edges are grouped into per-subsystem
incidents (incidents.py): the first edge gets a full Ghost brief via a one-shot
schedule_task IPC, new signatures get a short update, repeats are counted, and a
quiet incident closes with one plain line. Operator plant modes (plant_modes.py:
offline / maintenance / muted, muted liquefaction bits) suppress agent runs.
Loki polling is independent of Grafana Alerting / OnCall.

Usage:
    # Dry-run: print would-be IPC payloads, never write
    python3 monitor.py --dry-run

    # Normal (requires env / EnvironmentFile — see alert-monitor.env.example)
    python3 monitor.py

Env (see alert-monitor.env.example):
    LIQ_BASE_URL, BOP_BASE_URL, AUTH_TOKEN / LIQ_AUTH_TOKEN / BOP_AUTH_TOKEN,
    TARGET_JID, IPC_GROUP_FOLDER, NANOCLAW_ROOT, COOLDOWN_S, POLL_INTERVAL_S,
    GRAFANA_URL + GRAFANA_SERVICE_ACCOUNT_TOKEN (or LOKI_URL/USER/TOKEN) for
    Ghost option B Loki abort polling, LOKI_POLL_INTERVAL_S,
    LIQ_SOURCE (prom|api), LIQ_PROM_POLL_S, LIQ_STALE_S,
    INCIDENT_QUIET_S, INCIDENT_UPDATE_MIN_S (defaults to COOLDOWN_S)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

import plant_modes
from conditions import decode_bits, decode_conditions
from incidents import Incident, IncidentBook
from loki_abort import LokiAbortClient, line_is_watchdog, new_hits_after, watchdog_name
from container_phase import phase_summary, snapshot_filter_controls
from mute_state import is_muted
from nanoclaw_ipc import schedule_once, send_message
from plant_alerts_log import record_alert_event
from prom_source import PromClient
from prompt import build_prompt
from recent_deploy import (
    DEFAULT_RECENT_DEPLOY_WINDOW_S,
    build_recent_deploy,
    extract_systick,
    host_restart_check,
)

# Defaults match pi_alert / production layout on bob49
DEFAULT_LIQ_BASE = "http://10.3.0.183:3000"
DEFAULT_BOP_BASE = "http://10.3.0.94:3000"
DEFAULT_IPC_FOLDER = "telegram_main"
# Plant channel: Carbon Capture (telegram_carbon-capture)
DEFAULT_TARGET_JID = "tg:-1004294109828"
# Empty = chat default (no Telegram forum topic)
DEFAULT_TARGET_THREAD_ID = ""

COMMAND_ACTUAL_PAIRS = [
    ("column_heater", "column_heater_actual"),
    ("haskell_inlet", "haskell_inlet_actual"),
    ("haskell_control", "haskell_control_actual"),
    ("co2_sensor_zero", "co2_sensor_zero_actual"),
    ("tc_probe_heaters_enable", "tc_probe_heaters_enable_actual"),
    ("chiller_n20c_power", "chiller_n20c_power_actual"),
    ("chiller_5c_power", "chiller_5c_power_actual"),
    ("o2_sensor_power", "o2_sensor_power_actual"),
    ("spare_k7", "spare_k7_actual"),
]

LIQ_SENSOR_KEYS = [
    "system_state",
    "system_state_code",
    "o2_ppm",
    "accumulator_pressure_psi",
    "holding_tank_pressure_psi",
    "column_vent_flow_rate_slpm",
    "active_warnings",
    "active_errors",
]

BOP_SENSOR_KEYS = [
    "co2_accumulator_dry_pressure",
    "co2_accumulator_dry_dew_point",
    "extraction_manifold_pressure",
    "purge_manifold_pressure",
    "product_co2_ppm",
    "product_co2_flow",
    "compressor_power",
]


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


ET = ZoneInfo("America/New_York")
HTTP_BACKOFF_S = 60.0
# url -> (down since, retry after); one log line per outage instead of one per poll
_unreachable: dict[str, tuple[float, float]] = {}


def et_hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M ET")


def fetch_latest(url: str, label: str, token: str) -> dict[str, Any] | None:
    now = time.time()
    down = _unreachable.get(url)
    if down and now < down[1]:
        return None
    try:
        resp = requests.get(url, headers=auth_headers(token), timeout=5)
        if resp.status_code == 400 and not token:
            print(
                f"[ERROR] {label}: HTTP 400 — set AUTH_TOKEN / LIQ_AUTH_TOKEN / "
                f"BOP_AUTH_TOKEN (Bearer required)",
                flush=True,
            )
            return None
        resp.raise_for_status()
        data = resp.json()
        samples = data.get("data", [])
        if down:
            print(f"[INFO] {label} reachable again after {int(now - down[0])}s", flush=True)
            _unreachable.pop(url, None)
        if not samples:
            print(f"[WARN] no {label} samples in response", flush=True)
            return None
        return samples[-1]
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        if not down:
            print(f"[ERROR] cannot reach {label} at {url}; retrying every "
                  f"{int(HTTP_BACKOFF_S)}s (logged once per outage)", flush=True)
        _unreachable[url] = (down[0] if down else now, now + HTTP_BACKOFF_S)
    except Exception as e:
        print(f"[ERROR] {label}: {e}", flush=True)
    return None


def pick(d: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {k: d.get(k) for k in keys}


# Liquefaction PLC system_state_code → name (matches GUI / prompt template).
LIQ_STATE_BY_CODE = {
    0: "OFF",
    1: "RUN",
    2: "DEBUG",
    3: "ERROR",
}


def _normalize_liq_state(sensors: dict[str, Any]) -> str | None:
    """Return OFF|DEBUG|RUN|ERROR when known, else None."""
    state = sensors.get("system_state")
    if isinstance(state, str) and state.strip():
        return state.strip().upper()
    code = sensors.get("system_state_code")
    try:
        return LIQ_STATE_BY_CODE.get(int(code))
    except (TypeError, ValueError):
        return None


def _liquefaction_in_debug(sensors: dict[str, Any]) -> bool:
    """True when liquefaction PLC is in DEBUG (code 2 / string DEBUG)."""
    return _normalize_liq_state(sensors) == "DEBUG"


def divergences(controls: dict, sensors: dict) -> list[dict[str, Any]]:
    out = []
    for cmd_key, actual_key in COMMAND_ACTUAL_PAIRS:
        cmd = controls.get(cmd_key)
        actual = sensors.get(actual_key)
        if cmd is None or actual is None:
            continue
        if cmd != actual:
            out.append(
                {"command": cmd_key, "commanded": cmd, "actual": actual}
            )
    return out


@dataclass
class CooldownBook:
    cooldown_s: float
    last_fire: dict[str, float] = field(default_factory=dict)

    def allow(self, key: str, now: float) -> bool:
        last = self.last_fire.get(key)
        if last is not None and (now - last) < self.cooldown_s:
            return False
        return True

    def mark(self, key: str, now: float) -> None:
        self.last_fire[key] = now


@dataclass
class MonitorState:
    liq_tower_warn: bool = False
    liq_tower_error: bool = False
    liq_warn_bits: set[int] = field(default_factory=set)
    liq_error_bits: set[int] = field(default_factory=set)
    # Normalized OFF|DEBUG|RUN|ERROR (None until primed / unknown)
    liq_system_state: str | None = None
    container_lock: bool = False
    interlock_names: set[str] = field(default_factory=set)
    # Loki automation abort cursor (ns); primed after first successful query
    loki_primed: bool = False
    loki_last_ts_ns: int = 0
    loki_next_poll_at: float = 0.0
    # After first sample, edges are meaningful
    liq_primed: bool = False
    bop_primed: bool = False
    # Prom-sourced liquefaction freshness
    liq_next_prom_poll_at: float = 0.0
    liq_last_sample_ts: float | None = None
    liq_last_systick: int | None = None
    liq_seen_fresh: bool = False
    liq_stale: bool = False
    liq_stale_alerted: bool = False
    liq_prom_error: bool = False


class AlertMonitor:
    def __init__(
        self,
        *,
        liq_base: str,
        bop_base: str,
        liq_token: str,
        bop_token: str,
        nanoclaw_root: Path,
        ipc_group_folder: str,
        target_jid: str,
        cooldown_s: float,
        poll_interval_s: float,
        dry_run: bool,
        recent_deploy_window_s: float = DEFAULT_RECENT_DEPLOY_WINDOW_S,
        target_thread_id: str | None = None,
        loki_client: LokiAbortClient | None = None,
        loki_poll_interval_s: float = 15.0,
        prom_client: PromClient | None = None,
        liq_prom_poll_s: float = 15.0,
        liq_stale_s: float = 180.0,
        incident_quiet_s: float = 900.0,
        incident_update_min_s: float | None = None,
        persist_incidents: bool = True,
    ) -> None:
        self.liq_telemetry = f"{liq_base.rstrip('/')}/api/v1/system/liquefaction/telemetry"
        self.liq_app_telemetry = f"{liq_base.rstrip('/')}/api/v1/telemetry"
        self.bop_telemetry = f"{bop_base.rstrip('/')}/api/v1/telemetry"
        self.liq_token = liq_token
        self.bop_token = bop_token
        self.nanoclaw_root = nanoclaw_root
        self.ipc_group_folder = ipc_group_folder
        self.target_jid = target_jid
        self.target_thread_id = target_thread_id
        self.poll_interval_s = poll_interval_s
        self.dry_run = dry_run
        self.recent_deploy_window_s = recent_deploy_window_s
        self.loki = loki_client
        self.loki_poll_interval_s = loki_poll_interval_s
        self.state = MonitorState()
        self.cooldown = CooldownBook(cooldown_s)
        self.prom = prom_client if (prom_client and prom_client.enabled) else None
        self.liq_prom_poll_s = liq_prom_poll_s
        self.liq_stale_s = liq_stale_s
        self.incidents = IncidentBook(
            nanoclaw_root,
            quiet_s=incident_quiet_s,
            update_min_s=cooldown_s if incident_update_min_s is None else incident_update_min_s,
            persist=persist_incidents and not dry_run,
        )
        # Latest alert per subsystem, for updates released after the rate limit
        self._latest_alert: dict[str, dict[str, Any]] = {}

    def snapshot_recent_deploy(self) -> dict[str, Any]:
        """Probe both plant hosts' systick; rare (only on alert fire)."""
        threshold = self.recent_deploy_window_s
        checks = []
        if self.prom:
            liq_systick = self.state.liq_last_systick if not self.state.liq_stale else None
        else:
            liq_systick = extract_systick(
                fetch_latest(self.liq_app_telemetry, "liquefaction-app", self.liq_token)
            )
        checks.append(
            host_restart_check(
                subsystem="liquefaction",
                systick=liq_systick,
                threshold_s=threshold,
            )
        )
        bop_sample = fetch_latest(self.bop_telemetry, "container-app", self.bop_token)
        checks.append(
            host_restart_check(
                subsystem="container",
                systick=extract_systick(bop_sample),
                threshold_s=threshold,
            )
        )
        return build_recent_deploy(checks, threshold_s=threshold)

    def fire(self, alert: dict[str, Any], cooldown_keys: list[str]) -> None:
        """Route one alert edge through mute, plant modes and incidents."""
        now = time.time()
        if is_muted(self.nanoclaw_root):
            print(
                f"[MUTED] skip {alert.get('kind')} subsystem={alert.get('subsystem')} "
                f"(say @Ghost unmute alerts to resume)",
                flush=True,
            )
            return
        subsystem = str(alert.get("subsystem") or "unknown")
        mode = plant_modes.active_mode(self.nanoclaw_root, subsystem, now)
        alert_id = f"plant-alert-{int(now)}-{int(now * 1000) % 1000}"
        alert["alert_id"] = alert_id
        alert.setdefault("fired_at", datetime.now().astimezone().isoformat(timespec="seconds"))
        action, inc = self.incidents.on_event(
            subsystem, cooldown_keys, now, alert_id=alert_id,
            suppressed_by=mode["mode"] if mode else None,
        )
        alert["incident"] = self._incident_info(inc, new=cooldown_keys if action != "count" else [])
        if mode:
            alert["suppressed_by"] = {k: mode.get(k) for k in ("mode", "reason", "set_by", "set_at", "until")}
        if action != "count":
            self._archive(alert)
        if mode:
            print(f"[SUPPRESSED] {alert.get('kind')} subsystem={subsystem} mode={mode['mode']} "
                  f"incident={inc.id} action={action}", flush=True)
            if action == "open" and mode.get("notify") == "line":
                until = mode.get("until")
                self._send_line(
                    f"{subsystem} alert ({alert.get('kind')}) not investigated: {subsystem} is "
                    f"{mode['mode']}{f' until {until}' if until else ''} ({mode.get('reason') or 'no reason given'}).",
                    mode.get("chat_jid"),
                )
            return
        if action in ("count", "pending"):
            self._latest_alert[subsystem] = alert
            print(f"[INCIDENT] {action} {alert.get('kind')} subsystem={subsystem} incident={inc.id} "
                  f"events={inc.events} keys={cooldown_keys}", flush=True)
            return
        self._dispatch(alert, inc, update=action == "update")

    def _incident_info(self, inc: Incident, *, new: list[str]) -> dict[str, Any]:
        return {
            "id": inc.id,
            "opened_at": datetime.fromtimestamp(inc.opened_at).astimezone().isoformat(timespec="seconds"),
            "events": inc.events,
            "briefs": inc.briefs,
            "signatures": list(inc.signatures),
            "new_signatures": new,
        }

    def _archive(self, alert: dict[str, Any]) -> None:
        # Shared archive first so other sessions can see the edge even before
        # Ghost finishes the brief. task_id ties IPC ↔ log entry.
        try:
            archived = record_alert_event(
                self.nanoclaw_root,
                alert_id=alert["alert_id"],
                alert=alert,
                dry_run=self.dry_run,
            )
            print(
                f"[ARCHIVE] plant-alerts id={alert['alert_id']} "
                f"path={archived.get('latest_path')}",
                flush=True,
            )
        except Exception as e:
            print(f"[WARN] plant-alerts archive failed: {e}", flush=True)

    def _send_line(self, text: str, chat_jid: str | None = None) -> None:
        result = send_message(
            nanoclaw_root=self.nanoclaw_root,
            ipc_group_folder=self.ipc_group_folder,
            target_jid=chat_jid or self.target_jid,
            thread_id=self.target_thread_id if not chat_jid or chat_jid == self.target_jid else None,
            text=text,
            dry_run=self.dry_run,
        )
        print(f"[{'DRY-RUN' if self.dry_run else 'IPC'}] line {result['path']}: {text}", flush=True)

    def _dispatch(self, alert: dict[str, Any], inc: Incident, *, update: bool) -> None:
        """Enrich and hand the alert to Ghost (full brief or incident update)."""
        try:
            alert["recent_deploy"] = self.snapshot_recent_deploy()
        except Exception as e:
            alert["recent_deploy"] = {
                "likely": False,
                "available": False,
                "error": str(e),
                "note": "Failed to probe systick for recent deploy/restart",
            }
            print(f"[WARN] recent_deploy probe failed: {e}", flush=True)

        alert_id = alert["alert_id"]
        prompt = build_prompt(alert, alert_id=alert_id, update=update)
        result = schedule_once(
            nanoclaw_root=self.nanoclaw_root,
            ipc_group_folder=self.ipc_group_folder,
            target_jid=self.target_jid,
            thread_id=self.target_thread_id,
            prompt=prompt,
            task_id=alert_id,
            dry_run=self.dry_run,
        )
        self.incidents.mark_brief(inc, time.time())

        mode = "DRY-RUN" if self.dry_run else "IPC"
        rd = alert.get("recent_deploy") or {}
        print(
            f"[{mode}] fired {'update' if update else 'brief'} kind={alert.get('kind')} "
            f"subsystem={alert.get('subsystem')} incident={inc.id} "
            f"recent_deploy={rd.get('likely')} path={result['path']}",
            flush=True,
        )
        if self.dry_run:
            print(json_preview(result["payload"]), flush=True)

    def tick_incidents(self, now: float | None = None) -> None:
        """Release rate-limited updates, close quiet incidents, end expired modes."""
        now = time.time() if now is None else now
        for inc in self.incidents.due_updates(now):
            alert = self._latest_alert.pop(inc.subsystem, None)
            if alert is None:
                continue
            alert["incident"] = self._incident_info(
                inc, new=[k for k in inc.signatures if k not in alert["incident"]["signatures"]]
                or alert["incident"]["new_signatures"],
            )
            self._dispatch(alert, inc, update=True)
        for inc in self.incidents.close_quiet(now):
            self._latest_alert.pop(inc.subsystem, None)
            quiet_min = int(self.incidents.quiet_s // 60)
            print(f"[INCIDENT] closed {inc.subsystem} incident={inc.id} events={inc.events} "
                  f"briefs={inc.briefs} suppressed_by={inc.suppressed_by}", flush=True)
            if inc.suppressed_by or not inc.briefs:
                continue
            still = self._still_active(inc.subsystem)
            self._send_line(
                f"{inc.subsystem}: no new alert events for {quiet_min} min, closing the incident "
                f"opened {et_hhmm(inc.opened_at)} ({inc.events} event{'s' if inc.events != 1 else ''}, "
                f"{inc.briefs} brief{'s' if inc.briefs != 1 else ''}). {still}".strip()
            )
        for entry in plant_modes.expire(self.nanoclaw_root, now):
            if "bit" in entry:
                text = (f"{entry.get('subsystem')} {entry.get('severity')} bit {entry.get('bit')} "
                        f"mute ended; that bit alerts again.")
            else:
                text = (f"{entry.get('subsystem')} {entry.get('mode')} window ended "
                        f"({entry.get('reason') or 'no reason given'}); {entry.get('subsystem')} "
                        f"alerts are live again.")
            print(f"[MODE] expired {entry}", flush=True)
            self._send_line(text, entry.get("chat_jid"))

    def _still_active(self, subsystem: str) -> str:
        if subsystem == "liquefaction" and self.state.liq_primed:
            if self.state.liq_stale:
                return "Liquefaction telemetry is still not reporting."
            lit = [n for n, on in (("error", self.state.liq_tower_error),
                                   ("warning", self.state.liq_tower_warn)) if on]
            if lit:
                return f"Tower {' and '.join(lit)} light still on."
        if subsystem == "container" and self.state.bop_primed and self.state.container_lock:
            return "Supervisor is still locked."
        return ""

    def process_liquefaction(self, sample: dict[str, Any]) -> None:
        liq = sample.get("liquefaction") or sample.get("system", {}).get("liquefaction")
        if not liq:
            # Envelope may be {timestamp_us, liquefaction: {...}} or nested under system
            print("[WARN] liquefaction sample missing liquefaction object", flush=True)
            return

        controls = liq.get("controls") or {}
        sensors = liq.get("sensors") or {}

        tower_warn = bool(controls.get("tower_warning_indicator"))
        tower_error = bool(controls.get("tower_error_indicator"))
        warn_bits = set(decode_bits(sensors.get("active_warnings") or 0))
        error_bits = set(decode_bits(sensors.get("active_errors") or 0))
        system_state = _normalize_liq_state(sensors)
        in_debug = system_state == "DEBUG"
        in_error = system_state == "ERROR"

        if not self.state.liq_primed:
            self.state.liq_tower_warn = tower_warn
            self.state.liq_tower_error = tower_error
            self.state.liq_warn_bits = warn_bits
            self.state.liq_error_bits = error_bits
            self.state.liq_system_state = system_state
            self.state.liq_primed = True
            print(
                f"[LIQ] primed tower_warn={tower_warn} tower_error={tower_error} "
                f"warn_bits={sorted(warn_bits)} error_bits={sorted(error_bits)} "
                f"system_state={system_state} debug={in_debug}",
                flush=True,
            )
            return

        new_warn_bits = warn_bits - self.state.liq_warn_bits
        new_error_bits = error_bits - self.state.liq_error_bits
        tower_warn_rise = tower_warn and not self.state.liq_tower_warn
        tower_error_rise = tower_error and not self.state.liq_tower_error

        # Operator-muted HAZOP bits: drop them, and drop a tower rise when
        # every bit lit on that tower is muted.
        now = time.time()
        muted_warn = plant_modes.muted_bits(self.nanoclaw_root, "liquefaction", "warning", now)
        muted_error = plant_modes.muted_bits(self.nanoclaw_root, "liquefaction", "error", now)
        if muted_warn or muted_error:
            new_warn_bits -= muted_warn
            new_error_bits -= muted_error
            if tower_warn_rise and warn_bits and warn_bits <= muted_warn:
                tower_warn_rise = False
            if tower_error_rise and error_bits and error_bits <= muted_error:
                tower_error_rise = False
        # Any transition into ERROR (RUN/OFF/DEBUG → ERROR), including cases
        # where the tower light / HAZOP mask did not edge (active_errors can
        # stay 0 while system_state_code becomes 3).
        prev_state = self.state.liq_system_state
        state_error_rise = in_error and prev_state != "ERROR"

        # Bit-set edges only while the corresponding tower light is on
        # (or rising this poll), so we don't spam from mask-only noise when
        # the GUI has dismissed the tower.
        bit_warn_fire = bool(new_warn_bits) and (tower_warn or tower_warn_rise)
        bit_error_fire = bool(new_error_bits) and (tower_error or tower_error_rise)

        # DEBUG is an ops/maintenance mode — suppress warning pages (Ghost +
        # Grafana mirror). Errors still fire. State below still advances so
        # sticky warnings already lit in DEBUG do not edge-fire on exit.
        if in_debug:
            if tower_warn_rise or bit_warn_fire:
                print(
                    f"[LIQ] suppress warnings while DEBUG "
                    f"tower_warn_rise={tower_warn_rise} "
                    f"new_warn_bits={sorted(new_warn_bits)}",
                    flush=True,
                )
            tower_warn_rise = False
            bit_warn_fire = False
            new_warn_bits = set()

        if (
            tower_warn_rise
            or tower_error_rise
            or bit_warn_fire
            or bit_error_fire
            or state_error_rise
        ):
            kinds = []
            if tower_warn_rise:
                kinds.append("tower_warning")
            if tower_error_rise:
                kinds.append("tower_error")
            if state_error_rise:
                kinds.append("system_state_error")
            if bit_warn_fire and not tower_warn_rise:
                kinds.append("new_warning_bits")
            if bit_error_fire and not tower_error_rise:
                kinds.append("new_error_bits")

            if tower_warn_rise and tower_error_rise and len(kinds) == 2:
                kind = "tower_warning_and_error"
            elif len(kinds) == 1:
                kind = kinds[0]
            else:
                kind = "+".join(kinds)

            conditions = decode_conditions(
                sensors.get("active_warnings"), severity="warning"
            ) + decode_conditions(sensors.get("active_errors"), severity="error")
            new_conditions = decode_conditions(
                _bits_to_mask(new_warn_bits), severity="warning"
            ) + decode_conditions(_bits_to_mask(new_error_bits), severity="error")

            alert = {
                "subsystem": "liquefaction",
                "kind": kind,
                "tower": {"warning": tower_warn, "error": tower_error},
                "system_state": system_state,
                "previous_system_state": prev_state,
                "conditions": conditions,
                "new_conditions": new_conditions,
                "sensors": pick(sensors, LIQ_SENSOR_KEYS),
                "divergences": divergences(controls, sensors),
            }

            cooldown_keys: list[str] = []
            if tower_warn_rise:
                cooldown_keys.append("liquefaction:tower_warning")
            if tower_error_rise:
                cooldown_keys.append("liquefaction:tower_error")
            if state_error_rise:
                cooldown_keys.append("liquefaction:system_state_error")
            for b in sorted(new_warn_bits):
                if tower_warn or tower_warn_rise:
                    cooldown_keys.append(f"liquefaction:warn:bit{b}")
            for b in sorted(new_error_bits):
                if tower_error or tower_error_rise:
                    cooldown_keys.append(f"liquefaction:error:bit{b}")
            if not cooldown_keys:
                cooldown_keys = [f"liquefaction:{kind}"]

            self.fire(alert, cooldown_keys)

        self.state.liq_tower_warn = tower_warn
        self.state.liq_tower_error = tower_error
        self.state.liq_warn_bits = warn_bits
        self.state.liq_error_bits = error_bits
        self.state.liq_system_state = system_state

    def process_container(self, sample: dict[str, Any]) -> None:
        app = sample.get("app")
        if not app:
            print("[WARN] container sample missing app object", flush=True)
            return

        supervisor = app.get("supervisor") or {}
        lock = bool(supervisor.get("lock"))
        active_recovery = supervisor.get("active_recovery")
        interlocks = supervisor.get("triggered_interlocks") or []
        names = {
            (item.get("name") if isinstance(item, dict) else str(item))
            for item in interlocks
            if item
        }
        names.discard(None)  # type: ignore[arg-type]
        names = {n for n in names if n}

        system = app.get("system") or {}
        bop = system.get("bop") or {}
        bop_sensors = bop.get("sensors") or {}

        if not self.state.bop_primed:
            self.state.container_lock = lock
            self.state.interlock_names = names
            self.state.bop_primed = True
            print(
                f"[BOP] primed lock={lock} interlocks={sorted(names)} "
                f"recovery={active_recovery!r}",
                flush=True,
            )
            return

        lock_rise = lock and not self.state.container_lock
        new_names = names - self.state.interlock_names
        # Skip interlock-only wakes while already locked (later polls)
        interlock_fire = bool(new_names) and (not self.state.container_lock or lock_rise)

        if lock_rise or interlock_fire:
            if lock_rise and new_names:
                kind = "supervisor_lock_and_interlock"
            elif lock_rise:
                kind = "supervisor_lock"
            else:
                kind = "triggered_interlock"

            alert = {
                "subsystem": "container",
                "kind": kind,
                "lock": lock,
                "active_recovery": active_recovery,
                "triggered_interlocks": interlocks,
                "new_interlocks": sorted(new_names),
                "phase": phase_summary(system),
                "filter_controls": snapshot_filter_controls(system),
                "bop_sensors": pick(bop_sensors, BOP_SENSOR_KEYS),
            }

            cooldown_keys: list[str] = []
            if lock_rise:
                cooldown_keys.append("container:lock")
            if interlock_fire:
                for n in sorted(new_names):
                    cooldown_keys.append(f"container:interlock:{n}")
            if not cooldown_keys:
                cooldown_keys = [f"container:{kind}"]

            self.fire(alert, cooldown_keys)

        self.state.container_lock = lock
        self.state.interlock_names = names


    def process_automation_loki(self, now: float | None = None) -> None:
        """Rising-edge on new abort lines in automation-console.log via Loki."""
        if self.loki is None or not self.loki.enabled:
            return
        now = time.time() if now is None else now
        if now < self.state.loki_next_poll_at:
            return
        self.state.loki_next_poll_at = now + self.loki_poll_interval_s

        try:
            hits = self.loki.fetch_abort_hits(now=now)
        except Exception as e:
            print(f"[ERROR] Loki automation abort query: {e}", flush=True)
            return

        if not self.state.loki_primed:
            max_ts = max((h.ts_ns for h in hits), default=int(now * 1e9))
            self.state.loki_last_ts_ns = max_ts
            self.state.loki_primed = True
            print(
                f"[LOKI] primed automation-abort cursor_ts_ns={max_ts} "
                f"recent_hits={len(hits)}",
                flush=True,
            )
            return

        new = new_hits_after(hits, self.state.loki_last_ts_ns)
        if not new:
            return

        # Advance cursor even if cooldown suppresses fire
        self.state.loki_last_ts_ns = max(h.ts_ns for h in new)

        latest = new[-1]
        matched_lines = [h.line for h in new]
        phase = None
        filter_controls = None
        bop_sensors: dict[str, Any] = {}
        bop = fetch_latest(self.bop_telemetry, "container", self.bop_token)
        if bop is not None:
            app = bop.get("app") or {}
            system = app.get("system") or {}
            phase = phase_summary(system)
            filter_controls = snapshot_filter_controls(system)
            bop_sensors = pick(
                (system.get("bop") or {}).get("sensors") or {}, BOP_SENSOR_KEYS
            )

        watchdog = line_is_watchdog(latest.line)
        name = watchdog_name(latest.line) if watchdog else None
        alert = {
            "subsystem": "container",
            "kind": "watchdog_trigger" if watchdog else "job_failed",
            "source": "loki",
            "job_id": "container_system",
            "matched_line": latest.line,
            "matched_lines": matched_lines,
            "match_count": len(new),
            "phase": phase,
            "filter_controls": filter_controls,
            "bop_sensors": bop_sensors,
        }
        if name:
            alert["watchdog"] = name
        cooldown_key = (
            f"container:watchdog:{name or 'unknown'}"
            if watchdog
            else "container:job_failed:log"
        )
        self.fire(alert, [cooldown_key])

    def poll_liquefaction_prom(self, now: float | None = None) -> None:
        """Liquefaction edges from Mimir, plus stopped / resumed reporting."""
        assert self.prom is not None
        now = time.time() if now is None else now
        if now < self.state.liq_next_prom_poll_at:
            return
        self.state.liq_next_prom_poll_at = now + self.liq_prom_poll_s
        try:
            got = self.prom.fetch_liquefaction()
        except Exception as e:
            if not self.state.liq_prom_error:
                print(f"[ERROR] liquefaction Prom query: {e} (logged once until it recovers)", flush=True)
            self.state.liq_prom_error = True
            return
        if self.state.liq_prom_error:
            print("[INFO] liquefaction Prom query recovered", flush=True)
            self.state.liq_prom_error = False
        st = self.state
        age = None if got.sample_ts is None else now - got.sample_ts
        if age is None or age > self.liq_stale_s:
            if not st.liq_stale:
                st.liq_stale = True
                last = got.sample_ts or st.liq_last_sample_ts
                if st.liq_seen_fresh:
                    st.liq_stale_alerted = True
                    self.fire(
                        {
                            "subsystem": "liquefaction",
                            "kind": "stopped_reporting",
                            "source": "prom",
                            "last_sample_at": plant_modes.iso(last) if last else None,
                            "age_s": int(now - last) if last else None,
                            "last_state": st.liq_system_state,
                            "tower": {"warning": st.liq_tower_warn, "error": st.liq_tower_error},
                            "note": "No new subsystem=\"liquefaction\" samples in Mimir; "
                                    "liquefaction alerting is blind until they return.",
                        },
                        ["liquefaction:stopped_reporting"],
                    )
                else:
                    print(f"[LIQ] telemetry stale at startup (last sample "
                          f"{plant_modes.iso(last) if last else 'none in 5 min'}); priming silently",
                          flush=True)
            return
        if st.liq_stale:
            gap_from = st.liq_last_sample_ts
            if st.liq_stale_alerted and not plant_modes.active_mode(self.nanoclaw_root, "liquefaction", now):
                gap = f" after {int((got.sample_ts - gap_from) // 60)} min" if gap_from else ""
                self._send_line(f"liquefaction telemetry is reporting again{gap}; alerting resumed.")
            print(f"[LIQ] telemetry fresh again (age {int(age)}s); re-priming edges", flush=True)
            st.liq_stale = False
            st.liq_stale_alerted = False
            st.liq_primed = False
        st.liq_seen_fresh = True
        st.liq_last_systick = got.systick
        if got.sample_ts == st.liq_last_sample_ts:
            return
        st.liq_last_sample_ts = got.sample_ts
        self.process_liquefaction(got.sample or {})

    def loop_once(self) -> None:
        if self.prom:
            try:
                self.poll_liquefaction_prom()
            except Exception as e:
                print(f"[ERROR] processing liquefaction (prom): {e}", flush=True)
        else:
            liq = fetch_latest(self.liq_telemetry, "liquefaction", self.liq_token)
            if liq is not None:
                try:
                    self.process_liquefaction(liq)
                except Exception as e:
                    print(f"[ERROR] processing liquefaction: {e}", flush=True)

        bop = fetch_latest(self.bop_telemetry, "container", self.bop_token)
        if bop is not None:
            try:
                self.process_container(bop)
            except Exception as e:
                print(f"[ERROR] processing container: {e}", flush=True)

        try:
            self.process_automation_loki()
        except Exception as e:
            print(f"[ERROR] processing automation Loki: {e}", flush=True)

        try:
            self.tick_incidents()
        except Exception as e:
            print(f"[ERROR] incident bookkeeping: {e}", flush=True)

    def run(self) -> None:
        print(
            f"Alert monitor starting\n"
            f"  liq: {'grafanacloud-prom (Mimir)' if self.prom else self.liq_telemetry} "
            f"stale_after={self.liq_stale_s}s\n"
            f"  bop: {self.bop_telemetry}\n"
            f"  loki_abort: "
            f"{'enabled' if (self.loki and self.loki.enabled) else 'disabled'} "
            f"poll={self.loki_poll_interval_s}s\n"
            f"  ipc: {self.nanoclaw_root}/data/ipc/{self.ipc_group_folder}/tasks\n"
            f"  targetJid: {self.target_jid}\n"
            f"  targetThreadId: {self.target_thread_id or '(chat default)'}\n"
            f"  dry_run={self.dry_run} poll={self.poll_interval_s}s "
            f"incident_update_min={self.incidents.update_min_s}s "
            f"incident_quiet={self.incidents.quiet_s}s "
            f"recent_deploy_window={self.recent_deploy_window_s}s "
            f"muted={is_muted(self.nanoclaw_root)} "
            f"modes={plant_modes.load(self.nanoclaw_root)['subsystems']}\n",
            flush=True,
        )
        while True:
            self.loop_once()
            time.sleep(self.poll_interval_s)


def _bits_to_mask(bits: set[int]) -> int:
    mask = 0
    for b in bits:
        mask |= 1 << b
    return mask


def json_preview(payload: dict[str, Any]) -> str:
    import json

    # Truncate long prompt in dry-run logs
    preview = dict(payload)
    p = preview.get("prompt")
    if isinstance(p, str) and len(p) > 500:
        preview["prompt"] = p[:500] + f"\n… [{len(p)} chars total]"
    return json.dumps(preview, indent=2)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect edges and print IPC payloads without writing files",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Single poll cycle then exit (useful with --dry-run)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    auth = os.environ.get("AUTH_TOKEN", "")
    nanoclaw_root = Path(
        os.environ.get(
            "NANOCLAW_ROOT",
            str(Path(__file__).resolve().parents[2]),
        )
    ).resolve()

    loki_client = LokiAbortClient(
        grafana_url=os.environ.get("GRAFANA_URL", ""),
        grafana_token=os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", ""),
        datasource_uid=os.environ.get("LOKI_DATASOURCE_UID", "grafanacloud-logs"),
        loki_url=os.environ.get("LOKI_URL", ""),
        loki_user=os.environ.get("LOKI_USER", ""),
        loki_token=os.environ.get("LOKI_TOKEN", ""),
        lookback_s=env_float("LOKI_LOOKBACK_S", 120.0),
    )
    # Optional: pull Grafana SA from mcp.env if not in alert-monitor.env
    if not loki_client.enabled:
        mcp_env = nanoclaw_root / "secrets" / "mcp.env"
        if mcp_env.is_file():
            for line in mcp_env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip("'").strip('"')
                if k == "GRAFANA_URL" and not loki_client.grafana_url:
                    loki_client.grafana_url = v.rstrip("/")
                elif (
                    k == "GRAFANA_SERVICE_ACCOUNT_TOKEN"
                    and not loki_client.grafana_token
                ):
                    loki_client.grafana_token = v

    monitor = AlertMonitor(
        liq_base=os.environ.get("LIQ_BASE_URL", DEFAULT_LIQ_BASE),
        bop_base=os.environ.get("BOP_BASE_URL", DEFAULT_BOP_BASE),
        liq_token=os.environ.get("LIQ_AUTH_TOKEN", auth),
        bop_token=os.environ.get("BOP_AUTH_TOKEN", auth),
        nanoclaw_root=nanoclaw_root,
        ipc_group_folder=os.environ.get("IPC_GROUP_FOLDER", DEFAULT_IPC_FOLDER),
        target_jid=os.environ.get("TARGET_JID", DEFAULT_TARGET_JID),
        target_thread_id=(
            os.environ.get("TARGET_THREAD_ID", DEFAULT_TARGET_THREAD_ID).strip()
            or None
        ),
        cooldown_s=env_float("COOLDOWN_S", 300.0),
        poll_interval_s=env_float("POLL_INTERVAL_S", 1.0),
        dry_run=args.dry_run or os.environ.get("DRY_RUN", "") == "1",
        recent_deploy_window_s=env_float(
            "RECENT_DEPLOY_WINDOW_S", DEFAULT_RECENT_DEPLOY_WINDOW_S
        ),
        loki_client=loki_client if loki_client.enabled else None,
        loki_poll_interval_s=env_float("LOKI_POLL_INTERVAL_S", 15.0),
        prom_client=(
            PromClient(
                grafana_url=loki_client.grafana_url,
                grafana_token=loki_client.grafana_token,
                datasource_uid=os.environ.get("PROM_DATASOURCE_UID", "grafanacloud-prom"),
            )
            if os.environ.get("LIQ_SOURCE", "prom") == "prom"
            else None
        ),
        liq_prom_poll_s=env_float("LIQ_PROM_POLL_S", 15.0),
        liq_stale_s=env_float("LIQ_STALE_S", 180.0),
        incident_quiet_s=env_float("INCIDENT_QUIET_S", 900.0),
        incident_update_min_s=(
            env_float("INCIDENT_UPDATE_MIN_S", 0.0) if os.environ.get("INCIDENT_UPDATE_MIN_S") else None
        ),
    )

    if args.once:
        monitor.loop_once()
        # Second poll so edges can be demonstrated after priming if state changes;
        # for --once we only prime + compare within one cycle (no edge on first).
        return 0

    try:
        monitor.run()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
