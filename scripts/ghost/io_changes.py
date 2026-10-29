"""Device I/O mapping (signal -> station/riser/pin) from co3ntrol-rs system code, and diffs between commits.

A remapped pin is the highest-suspicion kind of code change: the controller keeps
running, but writes go to a different coil and readbacks come from a different bit.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import common
import source_parse

SYSTEM_DIR = "control/src/system/"
# Pneumatic manifold table: panel riser OD pin -> manifold coil -> valve (hand-maintained wiring doc,
# kept in step with the code by co3ntrol-rs tools/io-topology/validate_io_table.py).
IO_DOCS = {"control/src/system/airbed.rs": ("documents/airbed-telemetry-io.md", "panel_station")}

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


def od_pins(io: dict[str, Any] | None) -> set[tuple[str, int, int]]:
    """(station, riser, pin) for every digital output the signal reads back or drives."""
    if not io:
        return set()
    pins = {(io["station"], io["riser"], p) for k, p in io.get("read") or [] if k == "od_bit"}
    ws, wr = io.get("write_station", io.get("station")), io.get("write_riser", io.get("riser"))
    pins |= {(ws, wr, p) for p in io.get("write") or []}
    return pins


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
            "pins_dropped": sorted(od_pins(a) - od_pins(b)),
            "pins_added": sorted(od_pins(b) - od_pins(a)),
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


def _coil(row: dict[str, Any] | None) -> str:
    return f"manifold {row['manifold_number']} pin {row['manifold_pin']}" if row else "no manifold row"


def annotate_coils(
    change: dict[str, Any], station: str, before_rows: list[dict[str, Any]], after_rows: list[dict[str, Any]]
) -> None:
    """Describe a remap in manifold-coil terms and flag moves onto/off spare or other valves' coils."""
    before = {(r["riser"], r["od_pin"]): r for r in before_rows}
    after = {(r["riser"], r["od_pin"]): r for r in after_rows}
    sig = change["signal"]
    added = [(r, p) for st, r, p in change.get("pins_added", []) if st == station]
    dropped = [(r, p) for st, r, p in change.get("pins_dropped", []) if st == station]
    coils, warnings = [], []
    for riser, pin in added:
        coils.append(f"now drives riser {riser} OD{pin} = {_coil(after.get((riser, pin)))}")
        prev = before.get((riser, pin))
        if prev and prev["valve"] != sig:
            owner = prev["valve"] or "UNUSED (spare coil)"
            warnings.append(f"riser {riser} OD{pin} ({_coil(prev)}) was documented as {owner} before this commit")
    for riser, pin in dropped:
        row = after.get((riser, pin)) or before.get((riser, pin))
        now = after.get((riser, pin))
        owner = f", documented {now['valve'] or 'UNUSED'} after this commit" if now and now["valve"] != sig else ""
        coils.append(f"no longer drives riser {riser} OD{pin} = {_coil(row)}{owner}")
    if added and dropped:
        warnings.append(
            "valve moved to a different coil: correct only if it was physically re-plumbed to the new coil; "
            "otherwise the valve is now undriven and the new pin drives whatever is on that coil"
        )
    if coils:
        change["coils"] = coils
    if warnings:
        change["coil_warnings"] = warnings


def mark_reverts(commits: list[dict[str, Any]]) -> None:
    """Tag remaps that undo an earlier commit's remap of the same signal (commits newest first)."""
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for c in reversed(commits):
        for r in (c.get("io") or {}).get("remaps", []):
            for older_c, older in seen:
                if older["signal"] == r["signal"] and older["before"] == r["after"] and older["after"] == r["before"]:
                    r["reverts"] = older_c["sha"]
                    older["reverted_by"] = {"sha": c["sha"], "committed": c["when"]}
            seen.append((c, r))


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
        doc = IO_DOCS.get(f)
        if file_remaps and doc:
            rows_before = source_parse.parse_manifold_table(common.git_show(repo, f"{sha}^", doc[0]) or "")
            rows_after = source_parse.parse_manifold_table(common.git_show(repo, sha, doc[0]) or "")
            for ch in file_remaps:
                annotate_coils(ch, doc[1], rows_before, rows_after)
                if rows_before != rows_after:
                    ch["wiring_doc_edited_in_same_commit"] = True
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
