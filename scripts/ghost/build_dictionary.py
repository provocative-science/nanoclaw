#!/usr/bin/env python3
"""Build Ghost's signal dictionary + system reference docs from code and deployed telemetry.

Field list = newest parquet schema per host (what is actually deployed).
Annotations = co3ntrol-rs source on the branch whose telemetry model best matches
that schema (deployed code often lives on a feature branch), GUI labels from
co3ntrol-gui main, IO tags/specs from the airbed IO documents, supervisor rules
from engine.rs + rules/**.

Outputs (read-only to agents at /workspace/ghost):
  data/ghost/signals.json
  data/ghost/systems/{airbed,container,liquefaction,supervisor}.md

Run: uv run --with duckdb --with boto3 python scripts/ghost/build_dictionary.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import source_parse as sp  # noqa: E402

RS = "co3ntrol-rs"
GUI = "co3ntrol-gui"
SCHEMA_HOSTS = {"airbed-host": "airbed", "bop-host": "bop"}
SYSTEM_DOCS = {"airbed": "airbed", "bop": "container", "dacs": "container", "liquefaction": "liquefaction"}
REF_WINDOW_DAYS = 45
MAX_REFS = 15


# ---------------------------------------------------------------------------
# Git refs
# ---------------------------------------------------------------------------


def candidate_refs(repo: Path) -> list[dict[str, str]]:
    out = common.git(
        repo,
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)\t%(objectname:short)\t%(committerdate:iso-strict)",
        "refs/remotes/origin",
    )
    cutoff = common.utcnow() - timedelta(days=REF_WINDOW_DAYS)
    refs: list[dict[str, str]] = []
    for line in out.splitlines():
        name, sha, when = line.split("\t")
        if name in ("origin/HEAD", "origin"):
            continue
        from datetime import datetime

        dt = datetime.fromisoformat(when)
        if name != "origin/main" and dt < cutoff:
            continue
        refs.append({"ref": name, "sha": sha, "date": when})
    main = [r for r in refs if r["ref"] == "origin/main"]
    others = [r for r in refs if r["ref"] != "origin/main"][: MAX_REFS - 1]
    return main + others


def telemetry_model(repo: Path, ref: str) -> tuple[dict[str, sp.RustStruct], dict[str, list[dict[str, Any]]]]:
    structs: dict[str, sp.RustStruct] = {}
    enums: dict[str, list[dict[str, Any]]] = {}
    files = ["models/src/telemetry.rs"] + [
        p for p in common.git_ls(repo, ref, "models/src/telemetry/") if p.endswith(".rs")
    ]
    for path in files:
        text = common.git_show(repo, ref, path)
        if text is None:
            continue
        structs.update(sp.parse_rust_structs(text))
        enums.update(sp.parse_rust_enums(text))
    return structs, enums


def leaf_paths(structs, enums) -> dict[str, sp.Leaf]:
    out: dict[str, sp.Leaf] = {}
    for leaf in sp.walk_leaves(structs, enums):
        path = []
        for seg in leaf.path:
            path.append(seg)
            if seg in sp.LABEL_DIMS:
                path.append(f"<{sp.LABEL_DIMS[seg]}_id>")
        out[".".join(path)] = leaf
    return out


# ---------------------------------------------------------------------------
# Deployed schema from S3
# ---------------------------------------------------------------------------


def deployed_schemas(store) -> dict[str, dict[str, Any]]:
    from paths import hive_prefix, parquet_s3_url

    s3 = store.s3_client()
    out: dict[str, dict[str, Any]] = {}
    for host in SCHEMA_HOSTS:
        try:
            src = store.resolve_source(s3, host)
        except LookupError as exc:
            common.log(f"schema {host}: {exc}")
            continue
        day = store.latest_partition_day(s3, "control", "telemetry", host, src.bucket, src.layout)
        if day is None:
            continue
        items: list[dict[str, Any]] = []
        for back in range(0, 3):
            d = day - timedelta(days=back)
            items += store.list_keys(s3, hive_prefix("control", "telemetry", host, d, layout=src.layout), 60, src.bucket)
        items.sort(key=lambda o: o["bytes"], reverse=True)
        leaves: dict[str, str] = {}
        sampled = []
        for item in items[:6]:
            url = parquet_s3_url(src.bucket, item["key"])
            try:
                _, rows = store.execute(f"DESCRIBE SELECT app FROM read_parquet('{url}')")
            except Exception as exc:  # unparsed stubs have no app column
                common.log(f"schema {host}: skip {item['key']}: {exc}")
                continue
            for path, ltype in sp.duck_type_leaves(rows[0][1]).items():
                if ltype in ("null", "list:null") and path in leaves:
                    continue
                leaves[path] = ltype
            sampled.append(item["key"])
        # A whole subsystem typed NULL (e.g. system.bop on the airbed host) is absent, not a leaf.
        leaves = {p: t for p, t in leaves.items() if not (t == "null" and p.count(".") <= 1 and p.startswith("system."))}
        normalized, instances = normalize_instances(leaves)
        out[host] = {
            "bucket": src.bucket,
            "layout": src.layout,
            "latest_day": day.isoformat(),
            "sampled_keys": sampled,
            "instances": instances,
            "leaves": normalized,
        }
    return out


def normalize_instances(leaves: dict[str, str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """JSON-inferred parquet stores dacs/filters maps as STRUCTs keyed by id; fold ids into <dim_id>."""
    out: dict[str, str] = {}
    instances: dict[str, set[str]] = defaultdict(set)
    for path, ltype in leaves.items():
        segs = path.split(".")
        norm: list[str] = []
        for i, seg in enumerate(segs):
            if i > 0 and segs[i - 1] in sp.LABEL_DIMS and not seg.startswith("<"):
                dim = sp.LABEL_DIMS[segs[i - 1]]
                instances[dim].add(seg)
                norm.append(f"<{dim}_id>")
            else:
                norm.append(seg)
        key = ".".join(norm)
        if key not in out or out[key] in ("null", "list:null"):
            out[key] = ltype
    return out, {k: sorted(v) for k, v in instances.items()}


def guess_deployed_ref(schema_leaves: dict[str, str], system: str, models: dict[str, dict[str, sp.Leaf]], refs: list[dict[str, str]]):
    prefix = f"system.{system}."
    deployed = {p for p in schema_leaves if p.startswith(prefix)}
    scored = []
    for r in refs:
        leaves = models.get(r["ref"])
        if not leaves:
            continue
        code = {p for p in leaves if p.startswith(prefix)}
        if not code:
            continue
        missing = len(deployed - code)
        extra = len(code - deployed)
        scored.append((-(missing + extra), r["ref"], missing, extra))
    if not scored:
        return None
    best = max(s[0] for s in scored)
    ties = [s for s in scored if s[0] == best]
    top = ties[0]
    return {
        "ref": top[1],
        "candidates": [s[1] for s in ties],
        "fields_not_in_code": top[2],
        "code_fields_not_deployed": top[3],
    }


# ---------------------------------------------------------------------------
# Supervisor rules
# ---------------------------------------------------------------------------


def supervisor_rules(repo: Path, ref: str) -> list[dict[str, Any]]:
    engine = common.git_show(repo, ref, "control/src/supervisor/engine.rs") or ""
    instances = sp.parse_engine_params(engine)
    defs: dict[tuple[str, str], dict[str, Any]] = {}
    for path in common.git_ls(repo, ref, "control/src/supervisor/rules/"):
        if not path.endswith(".rs") or path.endswith("rule_utils.rs"):
            continue
        rel = path[len("control/src/supervisor/rules/") : -3]
        module = "rules::" + rel.replace("/", "::")
        text = common.git_show(repo, ref, path) or ""
        for rule in sp.parse_rule_file(text, module):
            rule["file"] = path
            defs[(module, rule["type"])] = rule
    rules: list[dict[str, Any]] = []
    recoveries = {(m, r["type"]): r for (m, _), r in defs.items() if r["kind"] == "recovery"}
    for inst in instances:
        d = defs.get((inst["module"], inst["type"]))
        if d is None:
            continue
        rule = {k: v for k, v in d.items() if k != "source"}
        rule["params"] = inst["params"]
        if "priority" in inst["params"]:
            rule["priority"] = inst["params"]["priority"]
        rule["system"] = inst["module"].split("::")[1] if inst["module"].count("::") >= 2 else _infer_system(d)
        rule["ref"] = ref
        rule["source"] = d.get("source", "")
        if d["kind"] == "watchdog":
            actions = []
            for rec_name in d.get("recoveries", []):
                rec = next((r for (m, _), r in recoveries.items() if r["name"] == rec_name and m == inst["module"]), None)
                if rec:
                    actions.append({"recovery": rec_name, "actions": rec["actions"]})
            rule["recovery_actions"] = actions
            m = re.search(r"fn is_locking\(&self\)\s*->\s*bool\s*\{\s*(true|false)", d.get("source", ""))
            rule["is_locking"] = m.group(1) == "true" if m else True
        rules.append(rule)
    return rules


def _infer_system(rule: dict[str, Any]) -> str:
    systems = {i["signal"].split(".")[0] for i in rule.get("inputs", [])}
    if systems == {"airbed"}:
        return "airbed"
    if systems == {"liquefaction"}:
        return "liquefaction"
    return "container"


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------


def build(fetch: bool) -> dict[str, Any]:
    rs = common.CODEBASE_DIR / RS
    gui = common.CODEBASE_DIR / GUI
    if fetch:
        for repo in (rs, gui):
            common.git(repo, "fetch", "--prune", "-q", check=False, timeout=120)

    refs = candidate_refs(rs)
    models: dict[str, dict[str, sp.Leaf]] = {}
    enums_by_ref: dict[str, dict[str, Any]] = {}
    for r in refs:
        structs, enums = telemetry_model(rs, r["ref"])
        models[r["ref"]] = leaf_paths(structs, enums)
        enums_by_ref[r["ref"]] = enums

    store = common.import_store()
    schemas = deployed_schemas(store)

    deployed_guess: dict[str, Any] = {}
    for host, system in SCHEMA_HOSTS.items():
        if host in schemas:
            guess = guess_deployed_ref(schemas[host]["leaves"], system, models, refs)
            if guess:
                deployed_guess[host] = guess
    primary_ref = deployed_guess.get("airbed-host", {}).get("ref", "origin/main")
    ref_for_system = {
        "airbed": primary_ref,
        "bop": deployed_guess.get("bop-host", {}).get("ref", primary_ref),
    }

    # GUI
    gui_names: dict[str, str] = {}
    gui_sections: dict[str, str] = {}
    if gui.exists():
        disc = common.git_show(gui, "origin/main", "src/hooks/useDiscovery.ts") or ""
        gui_names = sp.parse_display_names(disc)
        for path in common.git_ls(gui, "origin/main", "src/components/"):
            if path.endswith(".tsx"):
                gui_sections.update(sp.parse_gui_sections(common.git_show(gui, "origin/main", path) or ""))

    # IO docs (airbed)
    io_tables = sp.parse_io_tables(common.git_show(rs, primary_ref, "documents/airbed-telemetry-io.md") or "")
    parts = sp.parse_parts_csv(
        common.git_show(rs, primary_ref, "documents/Airbed System Parts List - Electrical IO Assignments.csv") or ""
    )
    topo_signals: dict[str, dict[str, Any]] = {}
    topo_text = common.git_show(rs, primary_ref, "tools/io-topology/topology.json")
    if topo_text:
        for sig in json.loads(topo_text).get("signals", []):
            topo_signals[sig["name"]] = sig

    # Decode tables (liquefaction state names etc.)
    decodes: dict[str, dict[int, str]] = {}
    for path in common.git_ls(rs, "origin/main", "control/src/system/"):
        if path.endswith(".rs"):
            decodes.update(sp.decode_tables(common.git_show(rs, "origin/main", path) or ""))

    # Rules from every distinct deployed guess (+ main), deduped.
    rule_refs = list(dict.fromkeys([primary_ref, ref_for_system["bop"], "origin/main"]))
    rules: list[dict[str, Any]] = []
    seen_rules: dict[tuple, dict[str, Any]] = {}
    for ref in rule_refs:
        for rule in supervisor_rules(rs, ref):
            key = (rule["kind"], rule["name"], rule["system"], json.dumps(rule["params"], sort_keys=True))
            if key in seen_rules:
                seen_rules[key].setdefault("also_on", []).append(ref)
                continue
            seen_rules[key] = rule
            rules.append(rule)

    # Signals
    all_paths: dict[str, dict[str, Any]] = {}
    for host, schema in schemas.items():
        for path, ltype in schema["leaves"].items():
            entry = all_paths.setdefault(path, {"deployed_on": [], "schema_type": ltype})
            entry["deployed_on"].append(host)
            if entry["schema_type"] in ("null", "list:null"):
                entry["schema_type"] = ltype
    for ref in dict.fromkeys([primary_ref, ref_for_system["bop"], "origin/main"]):
        for path in models.get(ref, {}):
            all_paths.setdefault(path, {"deployed_on": [], "schema_type": ""})

    rule_index: dict[str, list[str]] = defaultdict(list)
    for rule in rules:
        for inp in rule.get("inputs", []):
            label = f"{rule['name']} ({rule['kind']}, {rule['system']})"
            if label not in rule_index[inp["signal"]]:
                rule_index[inp["signal"]].append(label)

    signals: list[dict[str, Any]] = []
    for path in sorted(all_paths):
        meta = all_paths[path]
        parts_path = path.split(".")
        system = parts_path[1] if parts_path[0] == "system" and len(parts_path) > 1 else parts_path[0]
        ref = ref_for_system.get(system, primary_ref)
        leaf = models.get(ref, {}).get(path)
        leaf_ref = ref
        if leaf is None:
            for r in refs:
                leaf = models.get(r["ref"], {}).get(path)
                if leaf is not None:
                    leaf_ref = r["ref"]
                    break
        field = parts_path[-1]
        kind = parts_path[-2] if len(parts_path) >= 2 and parts_path[-2] in ("controls", "sensors") else "state"
        json_path = [p for p in parts_path if not p.startswith("<")]
        prom, labels = sp.prom_name(json_path)
        sig: dict[str, Any] = {
            "id": ".".join(p for p in parts_path[1:] if not p.startswith("<")) if parts_path[0] == "system" else path,
            "system": system,
            "kind": kind,
            "field": field,
            "sql": sp.sql_path(json_path),
            "prom": prom,
            "type": (leaf.type if leaf else "") or meta["schema_type"],
            "deployed_on": meta["deployed_on"],
        }
        if any(p.startswith("<") for p in parts_path):
            dims = [p.strip("<>") for p in parts_path if p.startswith("<")]
            sig["prom_labels"] = labels
            sig["dims"] = dims
            inst: dict[str, list[str]] = {}
            for host in meta["deployed_on"]:
                for dim, ids in schemas[host].get("instances", {}).items():
                    if f"{dim}_id" in dims:
                        inst[dim] = sorted(set(inst.get(dim, [])) | set(ids))
            if inst:
                sig["instances"] = inst
        if leaf is not None:
            sig["source_ref"] = leaf_ref
            if leaf.field.section:
                sig["group"] = leaf.field.section
            doc = " ".join(x for x in (leaf.field.doc, leaf.field.comment) if x)
            if doc:
                sig["doc"] = doc
        else:
            sig["source_ref"] = None
            sig["note"] = "in deployed telemetry but not found in any recent co3ntrol-rs branch"
        if not meta["deployed_on"] and system in ("airbed", "bop"):
            sig["note"] = f"defined on {leaf_ref} but not present in deployed telemetry"
        if kind in ("controls", "sensors") or system in ("airbed", "bop", "dacs", "liquefaction"):
            sig["gui_label"] = sp.humanize(field, gui_names)
            if field in gui_sections:
                sig["gui_section"] = gui_sections[field]
        if system == "airbed":
            io = io_tables.get(field)
            if io:
                sig["tag"] = io["ref_des"]
                if io.get("control_type"):
                    sig["control_type"] = io["control_type"]
                part = parts.get(io["ref_des"])
                if part:
                    if part["name"]:
                        sig["description"] = part["name"]
                    if part["spec"]:
                        sig["spec"] = part["spec"]
                    if part["mpn"]:
                        sig["mpn"] = part["mpn"]
                    if part["notes"]:
                        sig["hw_notes"] = part["notes"]
            topo = topo_signals.get(field)
            if topo:
                sig["wired"] = topo.get("status") == "wired"
                if topo.get("where"):
                    sig["read_path"] = topo["where"]
                read = topo.get("read") or {}
                if read.get("station"):
                    sig["station"] = read["station"]
                if read.get("device"):
                    sig["device"] = read["device"]
                if topo.get("note"):
                    sig["io_note"] = topo["note"]
        units = sp.units_from_text(" ".join(str(sig.get(k, "")) for k in ("spec", "doc")))
        if units:
            sig["units"], sig["units_source"] = units, "spec/doc"
        else:
            guess = sp.units_heuristic(field, sig["type"])
            if guess:
                sig["units"], sig["units_source"] = guess, "name heuristic (unverified)"
        decode = decodes.get(field)
        if decode:
            sig["decode"] = {str(k): v for k, v in sorted(decode.items())}
        enum_name = sig["type"][5:] if sig["type"].startswith("enum:") else None
        if enum_name and enum_name in enums_by_ref.get(leaf_ref or "", {}):
            sig["enum"] = enums_by_ref[leaf_ref][enum_name]
        if sig["id"] in rule_index:
            sig["rules"] = rule_index[sig["id"]]
        signals.append(sig)

    host_system = {"airbed-host": "airbed", "bop-host": "container"}
    for rule in rules:
        rule["applies_to"] = [
            host
            for host, g in deployed_guess.items()
            if host_system.get(host) == rule["system"] and rule["ref"] in g.get("candidates", [g["ref"]])
        ]
        if not rule["applies_to"]:
            rule["note"] = "reference only: this ref does not match any host's deployed telemetry"

    # Automation jobs: parameters + state machines.
    jobs: list[dict[str, Any]] = []
    by_field = defaultdict(list)
    for s in signals:
        by_field[s["field"]].append(s)
    for path in common.git_ls(rs, primary_ref, "jobs/"):
        if not path.endswith(".py") or path.endswith("_test.py"):
            continue
        text = common.git_show(rs, primary_ref, path) or ""
        for field_name, comment in re.findall(r"(?:Sensor|Control)Name\.([A-Z0-9_]+)\)?[^#\n]*#\s*(.+)", text):
            for s in by_field.get(field_name.lower(), []):
                note = f"{path}: {comment.strip()}"
                s.setdefault("job_notes", [])
                if note not in s["job_notes"]:
                    s["job_notes"].append(note)
                if not s.get("units") or "unverified" in s.get("units_source", ""):
                    unit = sp.units_from_text(comment.split(";")[0])
                    if unit:
                        s["units"], s["units_source"] = unit, f"comment in {path}"
        params = sp.parse_job_params(text)
        states = sp.parse_state_enums(text)
        doc_m = re.match(r'^\s*"""(.*?)"""', text, re.S)
        if params or states:
            jobs.append(
                {
                    "file": path,
                    "doc": (doc_m.group(1).strip().splitlines()[0] if doc_m else ""),
                    "params": params,
                    "states": states,
                }
            )

    return {
        "generated_at": common.iso(common.utcnow()),
        "codebase": {
            RS: {"refs_considered": refs, "deployed_guess": deployed_guess, "annotation_ref": ref_for_system},
        },
        "hosts": {
            host: {k: v for k, v in schema.items() if k != "leaves"} | {"field_count": len(schema["leaves"]), "system": SCHEMA_HOSTS[host]}
            for host, schema in schemas.items()
        },
        "signals": signals,
        "rules": rules,
        "jobs": jobs,
    }


# ---------------------------------------------------------------------------
# Markdown docs
# ---------------------------------------------------------------------------


def _cell(v: Any) -> str:
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")


def render_system_doc(dictionary: dict[str, Any], doc_name: str) -> str:
    systems = [s for s, d in SYSTEM_DOCS.items() if d == doc_name]
    sigs = [s for s in dictionary["signals"] if s["system"] in systems]
    lines = [
        f"# {doc_name.title()} system reference (generated)",
        "",
        f"Generated {dictionary['generated_at']} by `scripts/ghost/build_dictionary.py`. Do not edit — regenerate.",
        "Use `mcp__telemetry__lookup_signal` for single lookups; this file is the browsable version.",
        "",
    ]
    guess = dictionary["codebase"][RS]["deployed_guess"]
    for host, g in guess.items():
        if SCHEMA_HOSTS.get(host) in systems:
            lines.append(
                f"- **{host}** deployed telemetry best matches `{g['ref']}` "
                f"({g['fields_not_in_code']} deployed fields not in that code, {g['code_fields_not_deployed']} code fields not deployed)."
            )
    for host, h in dictionary["hosts"].items():
        if h["system"] in systems:
            lines.append(f"- {host}: {h['field_count']} telemetry fields in `{h['bucket']}` ({h['layout']} layout), newest partition {h['latest_day']}.")
    if doc_name == "liquefaction":
        lines.append("- Liquefaction is not in S3 parquet; query live values in Grafana/Mimir with the `prom` names below.")
    lines.append("")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in sigs:
        groups[f"{s['kind']} — {s.get('gui_section') or s.get('group') or 'Other'}"].append(s)
    for title in sorted(groups):
        lines += [f"## {title}", "", "| GUI label | field (`sql`) | tag | type / units | notes |", "| --- | --- | --- | --- | --- |"]
        for s in groups[title]:
            notes = "; ".join(
                x
                for x in (
                    s.get("description"),
                    s.get("spec"),
                    s.get("read_path"),
                    "UNWIRED" if s.get("wired") is False else "",
                    "NOT DEPLOYED" if not s.get("deployed_on") and doc_name != "liquefaction" else "",
                    ("decode " + ", ".join(f"{k}={v}" for k, v in s["decode"].items())) if s.get("decode") else "",
                    ("rules: " + ", ".join(s["rules"])) if s.get("rules") else "",
                    s.get("doc"),
                )
                if x
            )
            lines.append(
                f"| {_cell(s.get('gui_label', ''))} | `{_cell(s['sql'])}` | {_cell(s.get('tag', ''))} | "
                f"{_cell(s['type'])} {_cell(s.get('units', ''))} | {_cell(notes)} |"
            )
        lines.append("")
    if doc_name == "airbed":
        jobs = [j for j in dictionary["jobs"] if "airbed" in j["file"]]
    elif doc_name == "container":
        jobs = [j for j in dictionary["jobs"] if "container" in j["file"]]
    else:
        jobs = []
    if jobs:
        lines += ["## Automation jobs (co3ntrol-rs `jobs/`)", ""]
        for j in jobs:
            lines.append(f"### `{j['file']}` — {j['doc']}")
            for enum_name, states in j["states"].items():
                lines.append(f"- States `{enum_name}`: {', '.join(states)}")
            for p in j["params"]:
                lines.append(f"- `{p['name']} = {p['value']}`" + (f" — {p['comment']}" if p["comment"] else ""))
            lines.append("")
    return "\n".join(lines) + "\n"


def render_supervisor_doc(dictionary: dict[str, Any]) -> str:
    lines = [
        "# Supervisor reference (generated)",
        "",
        f"Generated {dictionary['generated_at']}. Watchdogs run every tick; when one fires the engine starts its",
        "recovery and **latches `supervisor.lock`** (locking watchdogs). While locked, every unforced command is",
        "rejected with `can't accept unforced commands while locked`. The lock stays set until an operator calls",
        "clear_lock. Interlocks only *reject commands*; they never lock. Rule names are `pascal_to_snake(TypeName)`;",
        "airbed and container both define `extraction_bag_*` — check `system`.",
        "",
        "`get_if_valid(telem, N, …)` returns None when the newest non-null sample is N or more ticks old, so a",
        "field that goes null for N ticks counts as missing. Watchdogs with **missing input trips = yes** fire on",
        "missing/stale inputs, not only on out-of-range values — check for nulls before blaming the process.",
        "Use `mcp__telemetry__why_locked` to evaluate this automatically.",
        "",
        "Console log lines: `watchdog '<name>' triggered, starting recovery '<recovery>'`,",
        "`watchdog '<a>' (priority p) preempting recovery '<r>' (priority q)`, `recovery '<name>' completed`,",
        "`supervisor recovery action failed: <error>`.",
        "",
    ]
    for kind in ("watchdog", "interlock"):
        lines += [f"## {kind.title()}s", ""]
        for r in sorted((r for r in dictionary["rules"] if r["kind"] == kind), key=lambda r: -int(r.get("priority", 0) or 0)):
            lines.append(f"### `{r['name']}` ({r['system']}) — `{r['ref']}`" + (f", also on {', '.join(r['also_on'])}" if r.get("also_on") else ""))
            lines.append(f"- applies to: {', '.join(r['applies_to']) or r.get('note', 'none')}")
            if r.get("priority") is not None:
                lines.append(f"- priority {r['priority']}")
            lines.append(f"- params: `{json.dumps(r['params'])}`")
            for inp in r.get("inputs", []):
                extra = f" (stale after {inp['max_sample_age']} samples)" if inp.get("max_sample_age") is not None else ""
                lines.append(f"- input `{inp['signal']}` via {inp['accessor']}{extra}")
            if kind == "watchdog":
                lines.append(f"- missing input trips: {'yes' if r.get('missing_input_trips') else 'no'}")
                for ra in r.get("recovery_actions", []):
                    lines.append(f"- recovery `{ra['recovery']}` actions: {', '.join(ra['actions']) or '(none)'}")
            for msg in r.get("messages", [])[:8]:
                lines.append(f"- rejects with: \"{msg}\"")
            lines += ["", "```rust", r.get("source", "")[:2500], "```", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-fetch", action="store_true", help="skip git fetch of codebase repos")
    args = ap.parse_args()
    dictionary = build(fetch=not args.no_fetch)
    out = common.GHOST_DIR
    common.write_json_atomic(out / "signals.json", dictionary)
    for doc in sorted(set(SYSTEM_DOCS.values())):
        common.write_text_atomic(out / "systems" / f"{doc}.md", render_system_doc(dictionary, doc))
    common.write_text_atomic(out / "systems" / "supervisor.md", render_supervisor_doc(dictionary))
    common.log(
        f"dictionary: {len(dictionary['signals'])} signals, {len(dictionary['rules'])} rules, "
        f"deployed guess {json.dumps(dictionary['codebase'][RS]['deployed_guess'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
