#!/usr/bin/env python3
"""
Plant alert monitor for NanoClaw (runs on bob49 — not the Pi LED client).

Polls liquefaction tower indicators / HAZOP bitmasks / system_state ERROR
transitions, container supervisor lock + triggered_interlocks, and (option B)
Loki abort lines from Python automation-console.log. On rising edges, injects
a one-shot schedule_task IPC so Ghost messages the plant chat with context.
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
    Ghost option B Loki abort polling, LOKI_POLL_INTERVAL_S
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

import requests

from conditions import decode_bits, decode_conditions
from loki_abort import LokiAbortClient, new_hits_after
from container_phase import phase_summary, snapshot_filter_controls
from mute_state import is_muted
from nanoclaw_ipc import schedule_once
from plant_alerts_log import record_alert_event
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


def fetch_latest(url: str, label: str, token: str) -> dict[str, Any] | None:
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
        if not samples:
            print(f"[WARN] no {label} samples in response", flush=True)
            return None
        return samples[-1]
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] cannot reach server for {label}", flush=True)
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

    def snapshot_recent_deploy(self) -> dict[str, Any]:
        """Probe both plant hosts' systick; rare (only on alert fire)."""
        threshold = self.recent_deploy_window_s
        checks = []
        liq_sample = fetch_latest(
            self.liq_app_telemetry, "liquefaction-app", self.liq_token
        )
        checks.append(
            host_restart_check(
                subsystem="liquefaction",
                systick=extract_systick(liq_sample),
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
        now = time.time()
        if is_muted(self.nanoclaw_root):
            print(
                f"[MUTED] skip {alert.get('kind')} subsystem={alert.get('subsystem')} "
                f"(say @Ghost unmute alerts to resume)",
                flush=True,
            )
            return
        # All keys must be allowed; if any blocked, skip (avoid partial spam)
        if not all(self.cooldown.allow(k, now) for k in cooldown_keys):
            blocked = [k for k in cooldown_keys if not self.cooldown.allow(k, now)]
            print(f"[COOLDOWN] skip {alert.get('kind')} keys={blocked}", flush=True)
            return

        alert.setdefault("fired_at", datetime.now().astimezone().isoformat(timespec="seconds"))
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

        # Shared archive first so other sessions can see the edge even before
        # Ghost finishes the brief. task_id ties IPC ↔ log entry.
        alert_id = f"plant-alert-{int(time.time())}-{int(time.time() * 1000) % 1000}"
        alert["alert_id"] = alert_id
        try:
            archived = record_alert_event(
                self.nanoclaw_root,
                alert_id=alert_id,
                alert=alert,
                dry_run=self.dry_run,
            )
            print(
                f"[ARCHIVE] plant-alerts id={alert_id} "
                f"path={archived.get('latest_path')}",
                flush=True,
            )
        except Exception as e:
            print(f"[WARN] plant-alerts archive failed: {e}", flush=True)

        prompt = build_prompt(alert, alert_id=alert_id)
        result = schedule_once(
            nanoclaw_root=self.nanoclaw_root,
            ipc_group_folder=self.ipc_group_folder,
            target_jid=self.target_jid,
            thread_id=self.target_thread_id,
            prompt=prompt,
            task_id=alert_id,
            dry_run=self.dry_run,
        )
        for k in cooldown_keys:
            self.cooldown.mark(k, now)

        mode = "DRY-RUN" if self.dry_run else "IPC"
        rd = alert.get("recent_deploy") or {}
        print(
            f"[{mode}] fired kind={alert.get('kind')} subsystem={alert.get('subsystem')} "
            f"recent_deploy={rd.get('likely')} path={result['path']}",
            flush=True,
        )
        if self.dry_run:
            print(json_preview(result["payload"]), flush=True)

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

        alert = {
            "subsystem": "container",
            "kind": "job_failed",
            "source": "loki",
            "job_id": "container_system",
            "matched_line": latest.line,
            "matched_lines": matched_lines,
            "match_count": len(new),
            "phase": phase,
            "filter_controls": filter_controls,
            "bop_sensors": bop_sensors,
        }
        self.fire(alert, ["container:job_failed:log"])

    def loop_once(self) -> None:
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

    def run(self) -> None:
        print(
            f"Alert monitor starting\n"
            f"  liq: {self.liq_telemetry}\n"
            f"  bop: {self.bop_telemetry}\n"
            f"  loki_abort: "
            f"{'enabled' if (self.loki and self.loki.enabled) else 'disabled'} "
            f"poll={self.loki_poll_interval_s}s\n"
            f"  ipc: {self.nanoclaw_root}/data/ipc/{self.ipc_group_folder}/tasks\n"
            f"  targetJid: {self.target_jid}\n"
            f"  targetThreadId: {self.target_thread_id or '(chat default)'}\n"
            f"  dry_run={self.dry_run} poll={self.poll_interval_s}s "
            f"cooldown={self.cooldown.cooldown_s}s "
            f"recent_deploy_window={self.recent_deploy_window_s}s "
            f"muted={is_muted(self.nanoclaw_root)}\n",
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
