"""Device I/O mapping (signal -> station/riser/pin) from co3ntrol-rs system code, and diffs between commits.

A remapped pin is the highest-suspicion kind of code change: the controller keeps
running, but writes go to a different coil and readbacks come from a different bit.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import common

SYSTEM_DIR = "control/src/system/"

# `field: panel_station_riser_states[5]\n    .and_then(|v| Some(!v.od_bit(4)? && v.od_bit(5)?)),`
_READ_RE = re.compile(
    r"(?m)^\s*(?P<field>[a-z_][a-z0-9_]*)\s*:\s*(?P<station>[a-z_][a-z0-9_]*?)_riser_states\[(?P<riser>\d+)\]"
    r"\s*\.and_then\(\|\w+\|\s*(?P<expr>.*?)\)\s*,[ \t]*(?://[^\n]*)?$",
    re.S,
)
_PIN_RE = re.compile(r"\.(?P<kind>od_bit|di_bit|ntc_celsius|ain_volts|ain)\((?P<pin>\d+)\)")
_SETTER_RE = re.compile(r"(?m)^\s*pub (?:async )?fn set_(?P<field>[a-z_][a-z0-9_]*)\s*\(")
_WRITE_RE = re.compile(
    r"backplane::(?P<fn>set_od_pair|set_od)\(\s*&mut self\.ifc_modbus_(?P<station>[a-z_][a-z0-9_]*)\s*,"
    r"\s*(?P<riser>\d+)\s*,\s*(?P<a>\d+)\s*(?:,\s*(?P<b>\d+)\s*)?,"
)
_KIND_LABEL = {"od_bit": "OD", "di_bit": "DI", "ntc_celsius": "NTC", "ain_volts": "AIN", "ain": "AIN"}
# Pin-ish tokens worth quoting from other I/O diffs (LabJack registers, Modbus addresses, riser/pin args).
_PINISH_RE = re.compile(
    r"\b(?:FIO|EIO|CIO|MIO|DIO|AIN|DAC)\d+\b|od_bit\(|di_bit\(|set_od|riser|unit_id|address|register|coil|\bpin\b",
    re.I,
)


def _fmt_pins(pins: list[tuple[str, int]]) -> str:
    return "+".join(f"{_KIND_LABEL.get(k, k)}{p}" for k, p in pins)


def parse_io_map(text: str) -> dict[str, dict[str, Any]]:
    """{signal: {station, riser, read: [(kind, pin)], write: [pins], write_fn}} from one system .rs file."""
    out: dict[str, dict[str, Any]] = {}
    for m in _READ_RE.finditer(text):
        expr = m.group("expr")
        if "_riser_states[" in expr:  # regex ran past a field without .and_then
            continue
        pins = [(p.group("kind"), int(p.group("pin"))) for p in _PIN_RE.finditer(expr)]
        if not pins:
            continue
        out[m.group("field")] = {
            "station": m.group("station"),
            "riser": int(m.group("riser")),
            "read": pins,
        }
    setters = list(_SETTER_RE.finditer(text))
    for i, s in enumerate(setters):
        body = text[s.end() : setters[i + 1].start() if i + 1 < len(setters) else len(text)]
        w = _WRITE_RE.search(body[:1500])
        if not w:
            continue
        entry = out.setdefault(s.group("field"), {"station": w.group("station"), "riser": int(w.group("riser"))})
        entry["write_station"] = w.group("station")
        entry["write_riser"] = int(w.group("riser"))
        entry["write"] = [int(w.group("a"))] + ([int(w.group("b"))] if w.group("b") else [])
        entry["write_fn"] = w.group("fn")
    return out


def describe(io: dict[str, Any] | None) -> str:
    if not io:
        return "unmapped"
    parts = []
    if io.get("read"):
        parts.append(f"read {io['station']} riser {io['riser']} {_fmt_pins(io['read'])}")
    if io.get("write"):
        pins = "+".join(f"OD{p}" for p in io["write"])
        parts.append(f"write {io.get('write_station', io['station'])} riser {io.get('write_riser', io['riser'])} {pins}")
    return "; ".join(parts)


def riser_keys(io: dict[str, Any] | None) -> set[str]:
    if not io:
        return set()
    keys = set()
    if io.get("read"):
        keys.add(f"{io['station']}:{io['riser']}")
    if io.get("write"):
        keys.add(f"{io.get('write_station', io['station'])}:{io.get('write_riser', io['riser'])}")
    return keys


def _comparable(io: dict[str, Any] | None) -> tuple:
    if not io:
        return ()
    return (
        io.get("station"),
        io.get("riser"),
        tuple(map(tuple, io.get("read") or [])),
        io.get("write_station"),
        io.get("write_riser"),
        tuple(io.get("write") or []),
    )


def diff_io_maps(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    changes = []
    removed = {s: _comparable(before[s]) for s in set(before) - set(after)}
    added = {s: _comparable(after[s]) for s in set(after) - set(before)}
    renamed: dict[str, str] = {}
    for old, key in removed.items():
        new = next((n for n, k in added.items() if k == key and n not in renamed.values()), None)
        if new:
            renamed[old] = new
    for old, new in sorted(renamed.items()):
        changes.append(
            {
                "signal": new,
                "kind": "renamed",
                "renamed_from": old,
                "before": describe(before[old]),
                "after": describe(after[new]),
                "risers": sorted(riser_keys(after[new])),
            }
        )
    skip = set(renamed) | set(renamed.values())
    for sig in sorted((set(before) | set(after)) - skip):
        a, b = before.get(sig), after.get(sig)
        if _comparable(a) == _comparable(b):
            continue
        change: dict[str, Any] = {
            "signal": sig,
            "kind": "added" if not a else "removed" if not b else "remapped",
            "before": describe(a),
            "after": describe(b),
            "risers": sorted(riser_keys(a) | riser_keys(b)),
        }
        if b and b.get("write") and b.get("read"):
            read_od = sorted(p for k, p in b["read"] if k == "od_bit")
            if read_od and read_od != sorted(b["write"]):
                change["warning"] = "after this change the readback pins differ from the written pins"
        changes.append(change)
    return changes


def _pin_owners(io_map: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """'station:riser:ODn' -> signals using it (read or write) — spots a remap onto a pin another valve uses."""
    owners: dict[str, list[str]] = {}
    for sig, io in io_map.items():
        for kind, pin in io.get("read") or []:
            if kind == "od_bit":
                owners.setdefault(f"{io['station']}:{io['riser']}:OD{pin}", []).append(sig)
        for pin in io.get("write") or []:
            key = f"{io.get('write_station', io['station'])}:{io.get('write_riser', io['riser'])}:OD{pin}"
            if sig not in owners.get(key, []):
                owners.setdefault(key, []).append(sig)
    return owners


def diff_excerpt(patch: str, max_lines: int = 10) -> list[str]:
    """Changed lines in a unified diff that look like I/O addressing."""
    lines = []
    for line in patch.splitlines():
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        if _PINISH_RE.search(line):
            lines.append(re.sub(r"\s+", " ", line.strip())[:180])
            if len(lines) >= max_lines:
                break
    return lines


def commit_io_changes(repo: Path, sha: str, files: list[str]) -> dict[str, Any] | None:
    """Structured remaps for backplane-mapped systems plus a quoted excerpt for other I/O edits."""
    system_files = [f for f in files if f.startswith(SYSTEM_DIR) and f.endswith(".rs")]
    if not system_files:
        return None
    remaps: list[dict[str, Any]] = []
    excerpts: list[dict[str, str]] = []
    for f in system_files:
        old = common.git_show(repo, f"{sha}^", f) or ""
        new = common.git_show(repo, sha, f) or ""
        before, after = parse_io_map(old), parse_io_map(new)
        file_remaps = diff_io_maps(before, after)
        if file_remaps:
            owners = _pin_owners(after)
            for ch in file_remaps:
                io = after.get(ch["signal"]) or {}
                shared = set()
                for pin in io.get("write") or []:
                    key = f"{io.get('write_station', io.get('station'))}:{io.get('write_riser', io.get('riser'))}:OD{pin}"
                    shared |= set(owners.get(key, [])) - {ch["signal"]}
                if shared:
                    ch["pin_also_used_by"] = sorted(shared)
                ch["file"] = f
            remaps.extend(file_remaps)
        else:
            patch = common.git(repo, "show", "--format=", "-U0", sha, "--", f, check=False)
            budget = 10 - len(excerpts)
            if budget > 0:
                excerpts.extend({"file": f, "line": line} for line in diff_excerpt(patch, max_lines=budget))
    if not remaps and not excerpts:
        return None
    return {"remaps": remaps, "excerpt": excerpts[:10]}


def io_map_for_ref(repo: Path, ref: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in common.git_ls(repo, ref, SYSTEM_DIR):
        if f.endswith(".rs"):
            text = common.git_show(repo, ref, f) or ""
            for sig, io in parse_io_map(text).items():
                out[sig] = {**io, "file": f}
    return out
