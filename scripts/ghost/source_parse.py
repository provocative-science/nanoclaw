"""Parsers for co3ntrol-rs / co3ntrol-gui source used to build the signal dictionary.

Pure functions over source text so they can be unit-tested without git or S3.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Rust structs / enums (models/src/telemetry/*.rs)
# ---------------------------------------------------------------------------

_STRUCT_RE = re.compile(r"^\s*pub struct (\w+)\s*\{\s*$")
_ENUM_RE = re.compile(r"^\s*pub enum (\w+)\s*\{\s*$")
_FIELD_RE = re.compile(r"^\s*pub (\w+):\s*(.+?),?\s*(?://\s*(.*))?$")
_SECTION_RE = re.compile(r"^\s*//\s*(?!pub\b)([A-Z][^/]*?)\s*$")


@dataclass
class RustField:
    name: str
    type: str
    doc: str = ""
    section: str = ""
    comment: str = ""


@dataclass
class RustStruct:
    name: str
    doc: str = ""
    fields: list[RustField] = field(default_factory=list)


def parse_rust_structs(text: str) -> dict[str, RustStruct]:
    """Extract `pub struct X { pub f: T, ... }` with doc comments and section headers."""
    structs: dict[str, RustStruct] = {}
    lines = text.splitlines()
    pending_doc: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("///"):
            pending_doc.append(stripped[3:].strip())
            i += 1
            continue
        match = _STRUCT_RE.match(line)
        if not match:
            if stripped and not stripped.startswith("#["):
                pending_doc = []
            i += 1
            continue
        struct = RustStruct(match.group(1), doc=" ".join(pending_doc))
        pending_doc = []
        section = ""
        field_doc: list[str] = []
        i += 1
        while i < len(lines):
            body = lines[i]
            s = body.strip()
            if s.startswith("}"):
                break
            if s.startswith("///"):
                field_doc.append(s[3:].strip())
            elif _SECTION_RE.match(body):
                section = _SECTION_RE.match(body).group(1).strip()
            else:
                fm = _FIELD_RE.match(body)
                if fm:
                    struct.fields.append(
                        RustField(
                            name=fm.group(1),
                            type=fm.group(2).strip(),
                            doc=" ".join(field_doc),
                            section=section,
                            comment=(fm.group(3) or "").strip(),
                        )
                    )
                field_doc = []
            i += 1
        structs[struct.name] = struct
        i += 1
    return structs


def parse_rust_enums(text: str) -> dict[str, list[dict[str, Any]]]:
    """Unit-variant enums: name -> [{variant, value?, doc}] (tuple/struct variants listed by name)."""
    enums: dict[str, list[dict[str, Any]]] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _ENUM_RE.match(lines[i])
        if not match:
            i += 1
            continue
        name = match.group(1)
        variants: list[dict[str, Any]] = []
        doc: list[str] = []
        depth = 1
        i += 1
        while i < len(lines) and depth > 0:
            s = lines[i].strip()
            if depth == 1:
                if s.startswith("///"):
                    doc.append(s[3:].strip())
                else:
                    vm = re.match(r"^(\w+)\s*(?:=\s*(-?\d+))?\s*[,({]?", s)
                    if vm and s and not s.startswith(("#", "//", "}")):
                        entry: dict[str, Any] = {"variant": vm.group(1)}
                        if vm.group(2) is not None:
                            entry["value"] = int(vm.group(2))
                        if doc:
                            entry["doc"] = " ".join(doc)
                        variants.append(entry)
                        doc = []
            depth += s.count("{") - s.count("}")
            i += 1
        enums[name] = variants
    return enums


def unwrap_type(rust_type: str) -> tuple[str, str]:
    """Return (container, inner): Option<T> -> ("option", T); HashMap<K, V> -> ("map", V); Vec<T> -> ("vec", T)."""
    t = rust_type.strip()
    m = re.fullmatch(r"Option<(.+)>", t)
    if m:
        return "option", m.group(1).strip()
    m = re.fullmatch(r"(?:std::collections::)?(?:HashMap|BTreeMap)<\s*[^,]+,\s*(.+)>", t)
    if m:
        return "map", m.group(1).strip()
    m = re.fullmatch(r"Vec<(.+)>", t)
    if m:
        return "vec", m.group(1).strip()
    return "", t


SCALAR_TYPES = {
    "bool": "bool",
    "f32": "float",
    "f64": "float",
    "u8": "int",
    "u16": "int",
    "u32": "int",
    "u64": "int",
    "i8": "int",
    "i16": "int",
    "i32": "int",
    "i64": "int",
    "usize": "int",
    "String": "string",
}

LABEL_DIMS = {"dacs": "dac", "filters": "filter"}
TRANSPARENT = {"app", "system"}


@dataclass
class Leaf:
    path: list[str]  # JSON path from app, e.g. ["system","airbed","controls","vent_or_bag_valve"]
    type: str  # bool | float | int | string | enum:<Name> | list:<Struct>
    field: RustField
    struct: str
    dims: list[str]


def walk_leaves(
    structs: dict[str, RustStruct],
    enums: dict[str, list[dict[str, Any]]],
    root: str = "App",
) -> list[Leaf]:
    """Enumerate every scalar leaf reachable from `root`, mirroring serde JSON paths."""
    out: list[Leaf] = []

    def visit(struct_name: str, path: list[str], dims: list[str], seen: tuple[str, ...]) -> None:
        struct = structs.get(struct_name)
        if struct is None or struct_name in seen:
            return
        for f in struct.fields:
            kind, inner = unwrap_type(f.type)
            if kind == "option":
                kind2, inner2 = unwrap_type(inner)
                if kind2:
                    kind, inner = kind2, inner2
            next_dims = dims
            if kind == "map":
                next_dims = dims + [LABEL_DIMS.get(f.name, f.name)]
            if inner in SCALAR_TYPES:
                ltype = SCALAR_TYPES[inner]
                if kind == "vec":
                    ltype = f"list:{ltype}"
                out.append(Leaf(path + [f.name], ltype, f, struct_name, next_dims))
            elif inner in enums:
                out.append(Leaf(path + [f.name], f"enum:{inner}", f, struct_name, next_dims))
            elif inner in structs:
                if kind == "vec":
                    out.append(Leaf(path + [f.name], f"list:{inner}", f, struct_name, next_dims))
                else:
                    visit(inner, path + [f.name], next_dims, seen + (struct_name,))

    visit(root, [], [], ())
    return out


def sql_path(path: list[str]) -> str:
    """DuckDB struct path. Parquet stores dacs/filters as STRUCTs keyed by instance id
    (`app.system.dacs.a.filters.b.sensors.x`), so keyed segments get a `<dim_id>` placeholder."""
    out = "app"
    for seg in path:
        out += f".{seg}"
        if seg in LABEL_DIMS:
            out += f".<{LABEL_DIMS[seg]}_id>"
    return out


def prom_name(path: list[str]) -> tuple[str, list[str]]:
    """co3ntrol-rs /metrics flattening (control/src/api/metrics.rs)."""
    segs: list[str] = []
    labels: list[str] = []
    for seg in path:
        if seg in TRANSPARENT:
            continue
        if seg in LABEL_DIMS:
            segs.append(LABEL_DIMS[seg])
            labels.append(LABEL_DIMS[seg])
            continue
        segs.append(seg)
    return "co3ntrol_" + "_".join(segs), labels


def decode_tables(text: str) -> dict[str, dict[int, str]]:
    """`fn decode_<x>_name(v) -> Option<String> { match .. { 4 => "STARTUP".to_string(), .. } }`."""
    out: dict[str, dict[int, str]] = {}
    for m in re.finditer(r"fn decode_(\w+?)(?:_name)?\s*\([^)]*\)[^{]*\{(.*?)\n\}", text, re.S):
        arms = re.findall(r"(\d+)\s*=>\s*\"([^\"]+)\"", m.group(2))
        if arms:
            out[m.group(1)] = {int(k): v for k, v in arms}
    return out


# ---------------------------------------------------------------------------
# Supervisor rules (control/src/supervisor/rules/**.rs + engine.rs)
# ---------------------------------------------------------------------------


def pascal_to_snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


_ACCESSOR_RE = re.compile(
    r"rule_utils::(get_if_valid|avg_last_n_f32|last_valid)\(\s*telem,\s*([^|]*?)\|\s*t\s*\|\s*(.*?)\)\s*;",
    re.S,
)
_SYS_PATH_RE = re.compile(
    r"t\s*\.app\s*\.system\s*\.(\w+)(?:\s*\.as_ref\(\)\?)?\s*\.(sensors|controls)\s*\.(\w+)", re.S
)


def _impl_blocks(text: str) -> dict[tuple[str, str], str]:
    """(trait, type) -> body of `impl Trait for Type { ... }`."""
    blocks: dict[tuple[str, str], str] = {}
    for m in re.finditer(r"impl\s+(\w+)\s+for\s+(\w+)\s*\{", text):
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        blocks[(m.group(1), m.group(2))] = text[start : i - 1]
    return blocks


def _inputs(body: str) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for m in _ACCESSOR_RE.finditer(body):
        accessor, args, closure = m.group(1), m.group(2), m.group(3)
        pm = _SYS_PATH_RE.search(closure)
        if not pm:
            continue
        key = (pm.group(1), pm.group(2), pm.group(3))
        if key in seen:
            continue
        seen.add(key)
        nums = [a.strip() for a in args.split(",") if a.strip()]
        entry: dict[str, Any] = {
            "signal": ".".join(key),
            "accessor": accessor,
        }
        if accessor == "get_if_valid" and nums:
            entry["max_sample_age"] = int(nums[0]) if nums[0].isdigit() else nums[0]
        if accessor == "avg_last_n_f32" and len(nums) >= 2:
            entry["avg_n"], entry["min_valid"] = nums[0], nums[1]
        inputs.append(entry)
    # Direct field reads that bypass the helpers.
    for pm in _SYS_PATH_RE.finditer(body):
        key = (pm.group(1), pm.group(2), pm.group(3))
        if key not in seen:
            seen.add(key)
            inputs.append({"signal": ".".join(key), "accessor": "direct"})
    # DAC / filter instances reached through closures (`|dac| dac.controls.x`, `f.sensors.y`).
    for var, kind, name in _INSTANCE_RE.findall(body):
        base = "dacs.filters" if var in ("f", "filter", "filt") else "dacs"
        key = (base, kind, name)
        if key not in seen:
            seen.add(key)
            inputs.append({"signal": ".".join(key), "accessor": "per-instance"})
    return inputs


_INSTANCE_RE = re.compile(r"\b(f|filter|filt|dac|d)\s*\.\s*(sensors|controls)\s*\.\s*(\w+)")


def _recovery_actions(body: str) -> list[str]:
    return [
        f"{ctrl}({val.strip()})"
        for ctrl, val in re.findall(r"(?:Airbed|Bop|Dac|Filter|Liquefaction)Control::(\w+)\(([^)]*)\)", body)
    ]


_MISSING_TRIP_RES = (
    # `let Some(x) = x else { return Some(Box::new(Recovery…)) }` and `if let … {} else { return Some(…) }`
    re.compile(r"\belse\s*\{\s*return\s+Some\(\s*Box::new\("),
    # `match x { None => return Some(Box::new(…)), … }`
    re.compile(r"\bNone\s*=>\s*(?:\{\s*)?(?:return\s+)?Some\(\s*Box::new\("),
)


def missing_input_trips(body: str) -> bool:
    """True when the rule starts its recovery because an input is missing/stale."""
    return any(r.search(body) for r in _MISSING_TRIP_RES)


def parse_rule_file(text: str, module: str) -> list[dict[str, Any]]:
    """Interlocks, watchdogs and recoveries defined in one rule file."""
    rules: list[dict[str, Any]] = []
    for (trait, type_name), body in _impl_blocks(text).items():
        if trait == "Interlock":
            rules.append(
                {
                    "kind": "interlock",
                    "type": type_name,
                    "name": pascal_to_snake(type_name),
                    "module": module,
                    "inputs": _inputs(body),
                    "messages": sorted(set(re.findall(r'format!\(\s*"([^"]+)"', body)))[:20],
                    "source": body.strip()[:6000],
                }
            )
        elif trait == "WatchdogCondition":
            recoveries = sorted({pascal_to_snake(r) for r in re.findall(r"Box::new\((\w+Recovery)\b", body)})
            rules.append(
                {
                    "kind": "watchdog",
                    "type": type_name,
                    "name": pascal_to_snake(type_name),
                    "module": module,
                    "inputs": _inputs(body),
                    "recoveries": recoveries,
                    "missing_input_trips": missing_input_trips(body),
                    "source": body.strip()[:6000],
                }
            )
        elif trait == "RecoverySequence":
            rules.append(
                {
                    "kind": "recovery",
                    "type": type_name,
                    "name": pascal_to_snake(type_name),
                    "module": module,
                    "actions": _recovery_actions(body),
                    "source": body.strip()[:4000],
                }
            )
    return rules


def parse_engine_params(text: str) -> list[dict[str, Any]]:
    """Active rule instances in SupervisorEngine::new(): module path, type, params, list (interlocks|conditions)."""
    out: list[dict[str, Any]] = []
    for list_name in ("interlocks", "conditions"):
        m = re.search(rf"{list_name}:\s*vec!\[(.*?)\n\s*\],", text, re.S)
        if not m:
            continue
        body = "\n".join(
            line for line in m.group(1).splitlines() if not line.strip().startswith("//")
        )
        for inst in re.finditer(r"Box::new\(\s*(?:\n\s*)?rules::([\w:]+)::(\w+)\s*\{(.*?)\}\s*,?\s*\)", body, re.S):
            params: dict[str, Any] = {}
            for k, v in re.findall(r"(\w+):\s*([-\w.]+)", inst.group(3)):
                try:
                    params[k] = float(v) if "." in v else int(v)
                except ValueError:
                    params[k] = v
            out.append(
                {
                    "list": list_name,
                    "module": "rules::" + inst.group(1),
                    "type": inst.group(2),
                    "name": pascal_to_snake(inst.group(2)),
                    "params": params,
                }
            )
    return out


# ---------------------------------------------------------------------------
# GUI (co3ntrol-gui)
# ---------------------------------------------------------------------------


def parse_display_names(text: str) -> dict[str, str]:
    m = re.search(r"const DISPLAY_NAMES[^=]*=\s*\{(.*?)\n\};", text, re.S)
    if not m:
        return {}
    return dict(re.findall(r'^\s*(\w+):\s*"([^"]+)"', m.group(1), re.M))


def humanize(name: str, display_names: dict[str, str]) -> str:
    if name in display_names:
        return display_names[name]
    return re.sub(r"\b\w", lambda c: c.group(0).upper(), name.replace("_", " "))


def parse_gui_sections(text: str) -> dict[str, str]:
    """control id -> section title from `{ title: "...", controlIds: [ ... ] }` blocks."""
    out: dict[str, str] = {}
    for m in re.finditer(r'title:\s*"([^"]+)",\s*controlIds:\s*\[(.*?)\]', text, re.S):
        for cid in re.findall(r'"(\w+)"', m.group(2)):
            out.setdefault(cid, m.group(1))
    return out


# ---------------------------------------------------------------------------
# IO documentation (documents/*.md, parts-list CSV, tools/io-topology/topology.json)
# ---------------------------------------------------------------------------


def parse_io_tables(md: str) -> dict[str, dict[str, str]]:
    """field -> {ref_des, control_type?, table} from the airbed telemetry IO markdown."""
    out: dict[str, dict[str, str]] = {}
    section = ""
    header: list[str] = []
    for line in md.splitlines():
        if line.startswith("## "):
            section = line[3:].strip().lower()
            header = []
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if not header:
            header = [c.lower() for c in cells]
            continue
        row = dict(zip(header, cells))
        name = row.get("telemetry", "")
        if not name or section not in {"controls", "sensors"}:
            continue
        entry = {"ref_des": row.get("ref des", ""), "table": section}
        if row.get("control type"):
            entry["control_type"] = row["control type"]
        out[name] = entry
    return out


def parse_parts_csv(text: str) -> dict[str, dict[str, str]]:
    """RefDes -> {mpn, spec, name, notes} from the electrical IO assignments sheet."""
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = next((i for i, r in enumerate(rows) if r and r[0].strip() == "RefDes"), None)
    if header_idx is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    for r in rows[header_idx + 1 :]:
        if not r or not r[0].strip():
            continue
        ref = r[0].strip()
        out[ref] = {
            "mpn": (r[1] if len(r) > 1 else "").strip(),
            "spec": (r[2] if len(r) > 2 else "").strip(),
            "name": (r[3] if len(r) > 3 else "").strip(),
            "notes": (r[4] if len(r) > 4 else "").strip(),
        }
    return out


_UNIT_PATTERNS = [
    (re.compile(r"\bpsi\s*g\b|\bpsig\b", re.I), "psig"),
    (re.compile(r"\bpsi\s*a\b|\bpsia\b", re.I), "psia"),
    (re.compile(r"\bpsi\b", re.I), "psi"),
    (re.compile(r"\binHg\b", re.I), "inHg"),
    (re.compile(r"\btorr\b", re.I), "torr"),
    (re.compile(r"\bmbar\b", re.I), "mbar"),
    (re.compile(r"\bkpa\b", re.I), "kPa"),
    (re.compile(r"\bslpm\b", re.I), "SLPM"),
    (re.compile(r"\bgpm\b|\bl/min\b|\blpm\b", re.I), "flow"),
    (re.compile(r"°\s*C|\bdeg\s*C\b", re.I), "°C"),
    (re.compile(r"\bppm\b", re.I), "ppm"),
    (re.compile(r"\bHz\b"), "Hz"),
    (re.compile(r"(^|[;\s])V($|[;\s])"), "V"),
    (re.compile(r"\bmA\b"), "mA"),
    (re.compile(r"%\s*RH|\bRH\b"), "%RH"),
]


def units_from_text(text: str) -> str:
    for pattern, unit in _UNIT_PATTERNS:
        if pattern.search(text or ""):
            return unit
    return ""


def units_heuristic(name: str, ltype: str) -> str:
    if ltype == "bool":
        return "bool (1=on/open/true)"
    n = name.lower()
    if n.endswith("_frequency"):
        return "Hz"
    if n.endswith("_ppm") or "co2_ppm" in n:
        return "ppm"
    if n.endswith("_humidity"):
        return "%RH"
    if n.endswith("_temperature") or n.endswith("_temp"):
        return "°C?"
    if n.endswith("_pressure"):
        return "pressure (unit unverified)"
    if n.endswith("_flow"):
        return "flow (unit unverified)"
    return ""


# ---------------------------------------------------------------------------
# DuckDB parquet schema (deployed truth)
# ---------------------------------------------------------------------------

_DUCK_SCALARS = {
    "BOOLEAN": "bool",
    "FLOAT": "float",
    "DOUBLE": "float",
    "TINYINT": "int",
    "SMALLINT": "int",
    "INTEGER": "int",
    "BIGINT": "int",
    "UTINYINT": "int",
    "USMALLINT": "int",
    "UINTEGER": "int",
    "UBIGINT": "int",
    "HUGEINT": "int",
    "VARCHAR": "string",
    "JSON": "string",
}


def duck_type_leaves(type_str: str, prefix: list[str] | None = None) -> dict[str, str]:
    """Flatten a DuckDB STRUCT/MAP type string to {dot.path: leaf type}."""
    pos = 0
    s = type_str

    def skip_ws() -> None:
        nonlocal pos
        while pos < len(s) and s[pos] in " \t\n":
            pos += 1

    def ident() -> str:
        nonlocal pos
        skip_ws()
        if s[pos] == '"':
            end = pos + 1
            buf = ""
            while end < len(s):
                if s[end] == '"' and end + 1 < len(s) and s[end + 1] == '"':
                    buf += '"'
                    end += 2
                    continue
                if s[end] == '"':
                    break
                buf += s[end]
                end += 1
            pos = end + 1
            return buf
        m = re.compile(r"[A-Za-z_][\w]*").match(s, pos)
        if not m:
            raise ValueError(f"bad identifier at {pos}: {s[pos:pos + 30]!r}")
        pos = m.end()
        return m.group(0)

    def parse_type(path: list[str], out: dict[str, str]) -> str:
        nonlocal pos
        skip_ws()
        name = ident().upper()
        kind = name
        inner: dict[str, str] = {}
        if name == "STRUCT":
            skip_ws()
            assert s[pos] == "("
            pos += 1
            while True:
                skip_ws()
                if s[pos] == ")":
                    pos += 1
                    break
                field_name = ident()
                parse_type(path + [field_name], inner)
                skip_ws()
                if s[pos] == ",":
                    pos += 1
            kind = "struct"
        elif name == "MAP":
            skip_ws()
            pos += 1  # (
            parse_type(path + ["<key>"], {})
            skip_ws()
            pos += 1  # ,
            dim = LABEL_DIMS.get(path[-1], path[-1]) if path else "key"
            parse_type(path + [f"<{dim}_id>"], inner)
            skip_ws()
            pos += 1  # )
            kind = "map"
        else:
            skip_ws()
            if pos < len(s) and s[pos] == "(":
                depth = 0
                while pos < len(s):
                    if s[pos] == "(":
                        depth += 1
                    elif s[pos] == ")":
                        depth -= 1
                        if depth == 0:
                            pos += 1
                            break
                    pos += 1
            kind = _DUCK_SCALARS.get(name, name.lower())
        skip_ws()
        is_list = False
        while s.startswith("[]", pos):
            pos += 2
            is_list = True
        if is_list:
            out[".".join(path)] = f"list:{kind}"
            return f"list:{kind}"
        out.update(inner)
        if kind not in ("struct", "map") and path:
            out[".".join(path)] = kind
        return kind

    out: dict[str, str] = {}
    parse_type(prefix or [], out)
    return out


# ---------------------------------------------------------------------------
# Python automation jobs (jobs/**/*.py)
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"^([a-z_][a-z0-9_]*)\s*=\s*\(?\s*(-?[\d.]+|True|False|\"[^\"]*\")\s*\)?\s*(?:#\s*(.*))?$")


def parse_job_params(text: str) -> list[dict[str, str]]:
    """Module-level numeric/bool constants with their trailing comments (units/meaning)."""
    out: list[dict[str, str]] = []
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith((" ", "\t", "#")):
            continue
        m = _PARAM_RE.match(line.strip())
        if not m:
            # name = (\n value  # comment\n)
            m2 = re.match(r"^([a-z_][a-z0-9_]*)\s*=\s*\($", line.strip())
            if m2 and idx + 1 < len(lines):
                inner = re.match(r"^\s*(-?[\d.]+)\s*(?:#\s*(.*))?$", lines[idx + 1])
                if inner:
                    out.append({"name": m2.group(1), "value": inner.group(1), "comment": (inner.group(2) or "").strip()})
            continue
        out.append({"name": m.group(1), "value": m.group(2), "comment": (m.group(3) or "").strip()})
    return out


def parse_state_enums(text: str) -> dict[str, list[str]]:
    """`class XState(Enum): A = auto()` -> {XState: [A, ...]}."""
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"^class (\w+)\((?:\w+\.)?(?:Enum|IntEnum|StrEnum)\):\n((?:[ \t]+.*\n|\n)+)", text, re.M):
        names = re.findall(r"^[ \t]+([A-Z][A-Z0-9_]*)\s*=", m.group(2), re.M)
        if names:
            out[m.group(1)] = names
    return out
