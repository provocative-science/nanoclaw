"""Infer container pod cycle phase from DAC/filter valve telemetry.

Mirrors the operator-facing labels printed by
``co3ntrol-rs/jobs/container_system.py``:

- Begin filter X          (doors closing / sealed for the cycle)
- Purging filter X
- Extracting filter X
- Venting filter X
- Cooling filter X        (post-extract warm-loop cooldown)
- Emptying drain tank     (BOP purge vacuum briefly off)

Phases can overlap (e.g. extracting previous while purging next during the
bucket shuffle handoff).
"""

from __future__ import annotations

from typing import Any

FILTER_CONTROL_KEYS = (
    "doors",
    "inlet_warm_valve",
    "inlet_hot_valve",
    "outlet_warm_hot_valve",
    "purge_valve",
    "extraction_valve",
)


def _truthy(v: Any) -> bool:
    return bool(v) is True


def _filter_letter(fid: str) -> str:
    """Normalize filter ids like 'e' / 'E' / 'f1' to a display letter."""
    s = str(fid).strip()
    if not s:
        return "?"
    # Prefer a trailing single letter (a–h)
    for ch in reversed(s):
        if ch.isalpha():
            return ch.upper()
    return s.upper()


def snapshot_filter_controls(system: dict[str, Any]) -> dict[str, Any]:
    """Flatten dac→filter control bits for the alert payload / debugging."""
    out: dict[str, Any] = {"dacs": {}}
    dacs = (system or {}).get("dacs") or {}
    for dac_id, dac in dacs.items():
        if not isinstance(dac, dict):
            continue
        dac_entry: dict[str, Any] = {
            "controls": {
                k: (dac.get("controls") or {}).get(k)
                for k in ("purge_vent_valve", "extraction_vent_valve", "fan_enable")
            },
            "filters": {},
        }
        for fid, filt in (dac.get("filters") or {}).items():
            if not isinstance(filt, dict):
                continue
            ctrls = filt.get("controls") or {}
            dac_entry["filters"][str(fid)] = {
                k: ctrls.get(k) for k in FILTER_CONTROL_KEYS
            }
        out["dacs"][str(dac_id)] = dac_entry
    bop = (system or {}).get("bop") or {}
    bop_ctrls = bop.get("controls") or {}
    out["bop"] = {
        "purge_canal_valve": bop_ctrls.get("purge_canal_valve"),
        "purge_vacuum_enable": bop_ctrls.get("purge_vacuum_enable"),
    }
    return out


def infer_container_phases(system: dict[str, Any]) -> list[str]:
    """Return human-readable phase labels, highest-priority first.

    Labels match the style operators expect from container_system.py prints,
    e.g. ``Extracting Filter E``, ``Venting Filter B``.
    """
    phases: list[str] = []
    dacs = (system or {}).get("dacs") or {}

    extracting: list[str] = []
    venting: list[str] = []
    purging: list[str] = []
    beginning: list[str] = []
    cooling: list[str] = []

    for dac in dacs.values():
        if not isinstance(dac, dict):
            continue
        dac_ctrls = dac.get("controls") or {}
        purge_vent = _truthy(dac_ctrls.get("purge_vent_valve"))

        for fid, filt in (dac.get("filters") or {}).items():
            if not isinstance(filt, dict):
                continue
            c = filt.get("controls") or {}
            letter = _filter_letter(fid)
            extraction = _truthy(c.get("extraction_valve"))
            purge = _truthy(c.get("purge_valve"))
            doors_closed = c.get("doors") is False  # doors True = open at idle
            inlet_warm = _truthy(c.get("inlet_warm_valve"))
            inlet_hot = _truthy(c.get("inlet_hot_valve"))

            if extraction:
                extracting.append(letter)
            elif purge and purge_vent:
                venting.append(letter)
            elif purge:
                purging.append(letter)
            elif inlet_warm and not inlet_hot and doors_closed:
                cooling.append(letter)
            elif doors_closed and not inlet_hot and not inlet_warm:
                beginning.append(letter)

    for letter in sorted(extracting):
        phases.append(f"Extracting Filter {letter}")
    for letter in sorted(purging):
        phases.append(f"Purging Filter {letter}")
    for letter in sorted(venting):
        phases.append(f"Venting Filter {letter}")
    for letter in sorted(cooling):
        phases.append(f"Cooling Filter {letter}")
    for letter in sorted(beginning):
        phases.append(f"Begin Filter {letter}")

    bop = (system or {}).get("bop") or {}
    bop_ctrls = bop.get("controls") or {}
    if bop_ctrls.get("purge_vacuum_enable") is False:
        phases.append("Emptying drain tank")

    return phases


def phase_summary(system: dict[str, Any]) -> str:
    phases = infer_container_phases(system)
    if not phases:
        return "Idle / unknown (no active extract/purge/vent valves)"
    return "; ".join(phases)
