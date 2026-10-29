"""Unit tests for the Ghost host jobs: python3 -m unittest scripts/ghost/test_ghost.py"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = tempfile.mkdtemp(prefix="ghost-kb-")
os.environ["GHOST_KNOWLEDGE_DIR"] = _TMP
os.environ["GHOST_DATA_DIR"] = _TMP + "/ghost"

import kb  # noqa: E402
import kb_check  # noqa: E402
import kb_garden  # noqa: E402
import kb_recheck  # noqa: E402
import source_parse as sp  # noqa: E402

RULE = """
impl WatchdogCondition for WarmTankLevelWatchdog {
    fn priority(&self) -> u32 { self.priority }
    fn check(&self, telem: &VecDeque<telemetry::TimestampedApp>) -> Option<Box<dyn RecoverySequence>> {
        let warm_tank_level = rule_utils::get_if_valid(telem, 5, |t| {
            t.app.system.bop.as_ref()?.sensors.warm_tank_level
        });
        let Some(warm_tank_level) = warm_tank_level else {
            return Some(Box::new(WarmTankLevelRecovery { step: 0 }));
        };
        if warm_tank_level < self.min_level {
            return Some(Box::new(WarmTankLevelRecovery { step: 0 }));
        }
        None
    }
}
"""


class SourceParseTests(unittest.TestCase):
    def test_struct_fields_with_generic_commas(self) -> None:
        structs = sp.parse_rust_structs(
            "pub struct System {\n    /// DACs by id\n    pub dacs: HashMap<String, Dac>,\n    pub lock: bool, // latched\n}\n"
        )
        fields = {f.name: f for f in structs["System"].fields}
        self.assertEqual(fields["dacs"].type, "HashMap<String, Dac>")
        self.assertEqual(fields["lock"].comment, "latched")

    def test_names_and_paths(self) -> None:
        self.assertEqual(sp.pascal_to_snake("ExtractionBagWatchdog"), "extraction_bag_watchdog")
        self.assertEqual(sp.sql_path(["system", "dacs", "filters", "sensors", "x"]), "app.system.dacs.<dac_id>.filters.<filter_id>.sensors.x")
        name, labels = sp.prom_name(["system", "dacs", "sensors", "fan_power"])
        self.assertEqual((name, labels), ("co3ntrol_dac_sensors_fan_power", ["dac"]))

    def test_missing_input_trip_detection(self) -> None:
        self.assertTrue(sp.missing_input_trips(RULE))
        self.assertFalse(sp.missing_input_trips("if x < self.min { return Some(Box::new(R {})); } None"))
        tuple_form = "if let (Some(a), Some(b)) = (a, b) { } else {\n return Some((Box::new(R { step: 0 }), \"\".into()));\n }"
        self.assertTrue(sp.missing_input_trips(tuple_form))
        rules = sp.parse_rule_file(RULE, "bop")
        wd = next(r for r in rules if r["name"] == "warm_tank_level_watchdog")
        self.assertTrue(wd["missing_input_trips"])
        self.assertIn("warm_tank_level", json.dumps(wd["inputs"]))

    def test_decode_tables(self) -> None:
        text = 'fn decode_system_state_name(v: u16) -> Option<String> {\n    match v { 0 => "OFF".into(), 4 => "STARTUP".into(), _ => None }\n}\n'
        self.assertEqual(sp.decode_tables(text)["system_state"][4], "STARTUP")

    def test_duck_type_leaves(self) -> None:
        leaves = sp.duck_type_leaves("STRUCT(a STRUCT(b DOUBLE, c VARCHAR), d BOOLEAN)")
        self.assertEqual(leaves, {"a.b": "float", "a.c": "string", "d": "bool"})


def _status(entry_id: str, **extra) -> dict:
    now = datetime.now(timezone.utc)
    e = {
        "id": entry_id,
        "kind": "status",
        "title": "Container scrape down",
        "tags": ["container"],
        "systems": ["container"],
        "state": "active",
        "confidence": "likely",
        "author": "t",
        "source": "t",
        "created": kb.iso_ms(now),
        "updated": kb.iso_ms(now),
        "expires": kb.iso_ms(now + timedelta(days=1)),
        "ttl_days": 7,
        "body": "x",
    }
    e.update(extra)
    return e


class KnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        for p in kb.ENTRIES_DIR.glob("*.md"):
            p.unlink()

    def test_seed_files_round_trip_exactly(self) -> None:
        for p in sorted((HERE / "knowledge-seed").glob("*.md")):
            text = p.read_text(encoding="utf-8")
            self.assertEqual(kb.serialize_entry(kb.parse_entry(text)), text, p.name)

    def test_typescript_format_compatible(self) -> None:
        ts = 'tags: ["a"]\nsystems: []\n'
        self.assertIn(ts.splitlines()[0], kb.serialize_entry({"id": "k-1", "tags": ["a"], "systems": [], "body": "b"}))
        self.assertNotIn("systems", kb.serialize_entry({"id": "k-1", "tags": ["a"], "systems": [], "body": "b"}))

    def test_compare_ops(self) -> None:
        self.assertTrue(kb_check._compare("0.0", "==", 0))
        self.assertTrue(kb_check._compare(3, ">", 0))
        self.assertTrue(kb_check._compare("true", "==", True))

    def test_recheck_extends_resolves_and_queues(self) -> None:
        kb.write_entry(_status("k-hold", check={"type": "prom", "query": "q", "holds_when": False}))
        kb.write_entry(_status("k-gone", check={"type": "prom", "query": "q", "holds_when": False}))
        kb.write_entry(_status("k-nocheck", expires="2020-01-01T00:00:00.000Z"))
        results = {
            "k-hold": {"at": "x", "result": "holds", "detail": "still down"},
            "k-gone": {"at": "x", "result": "no_longer_holds", "detail": "up again"},
        }
        with mock.patch.object(kb_check, "evaluate", side_effect=lambda e: results[e["id"]]), mock.patch.object(kb, "commit"):
            counts = kb_recheck.recheck()
        self.assertEqual(counts["extended"], 1)
        self.assertEqual(counts["resolved"], 1)
        self.assertEqual(counts["review"], 1)
        hold = kb.read_entry("k-hold")
        self.assertGreater(kb.parse_time(hold["expires"]), datetime.now(timezone.utc) + timedelta(days=6))
        self.assertEqual(kb.read_entry("k-gone")["state"], "resolved")
        self.assertEqual(kb.read_entry("k-nocheck")["state"], "needs_review")

    def test_garden_queue_and_review_message(self) -> None:
        kb.write_entry(_status("k-a", kind="lesson", title="Warm tank watchdog trips below 14 when pumps run"))
        kb.write_entry(_status("k-b", kind="lesson", title="Warm tank watchdog trips below 14 when the pumps run"))
        kb.write_entry(_status("k-c", state="needs_review", review_reason="expired; no machine check"))
        items = kb_garden.build_queue()
        ids = {i["id"] for i in items}
        self.assertIn("k-c", ids)
        self.assertTrue({"k-a", "k-b"} & ids)
        text = kb_garden.format_review(items)
        self.assertIn("still <id>", text)
        self.assertIn("k-c", text)

    def test_review_goes_to_main_group_only(self) -> None:
        kb.write_entry(_status("k-c", state="needs_review", review_reason="expired"))
        sent = []
        with mock.patch.object(kb_garden, "main_group", return_value={"jid": "tg:spark", "folder": "telegram_main", "name": "Spark"}), \
             mock.patch.object(kb_garden, "send_ipc_message", side_effect=lambda jid, text, via_folder: sent.append((jid, via_folder))), \
             mock.patch.object(sys, "argv", ["kb_garden.py", "--review"]):
            kb_garden.main()
        self.assertEqual(sent, [("tg:spark", "telegram_main")])


class RunJobsTests(unittest.TestCase):
    def test_usage(self) -> None:
        proc = subprocess.run([str(HERE / "run_jobs.sh"), "nope"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


AIRBED_IO = """
            controls: telemetry::AirbedControls {
                vent_or_bag_valve: panel_station_riser_states[5]
                    .and_then(|v| Some(!v.od_bit(4)? && v.od_bit(5)?)),
                desorb_tube_extraction_gas_out_valve: panel_station_riser_states[5].and_then(|v| v.od_bit(PIN)),
                hot_in_temperature: panel_station_riser_states[0]
                    .and_then(|v| v.ntc_celsius(0)), // B14
            }
    pub async fn set_vent_or_bag_valve(&mut self, value: bool) -> Result<(), AirbedError> {
        match backplane::set_od_pair(&mut self.ifc_modbus_panel_station, 5, 4, 5, value).await {
            Ok(_) => Ok(()),
        }
    }

    pub async fn set_desorb_tube_extraction_gas_out_valve(&mut self, value: bool) -> Result<(), AirbedError> {
        match backplane::set_od(&mut self.ifc_modbus_panel_station, 5, PIN, value).await {
            Ok(_) => Ok(()),
        }
    }
"""


class IoChangeTests(unittest.TestCase):
    def setUp(self):
        import io_changes

        self.ic = io_changes

    def test_parse_reads_and_writes(self):
        m = self.ic.parse_io_map(AIRBED_IO.replace("PIN", "7"))
        self.assertEqual(m["vent_or_bag_valve"]["read"], [("od_bit", 4), ("od_bit", 5)])
        self.assertEqual(m["vent_or_bag_valve"]["write"], [4, 5])
        self.assertEqual(m["desorb_tube_extraction_gas_out_valve"]["write"], [7])
        self.assertEqual(m["hot_in_temperature"]["read"], [("ntc_celsius", 0)])
        self.assertEqual(self.ic.riser_keys(m["vent_or_bag_valve"]), {"panel_station:5"})

    def test_diff_detects_pin_swap(self):
        before = self.ic.parse_io_map(AIRBED_IO.replace("PIN", "7"))
        after = self.ic.parse_io_map(AIRBED_IO.replace("PIN", "6"))
        changes = self.ic.diff_io_maps(before, after)
        self.assertEqual(len(changes), 1)
        ch = changes[0]
        self.assertEqual((ch["signal"], ch["kind"], ch["risers"]), ("desorb_tube_extraction_gas_out_valve", "remapped", ["panel_station:5"]))
        self.assertIn("OD7", ch["before"])
        self.assertIn("OD6", ch["after"])

    def test_rename_is_not_a_remap(self):
        before = self.ic.parse_io_map(AIRBED_IO.replace("PIN", "7"))
        after = self.ic.parse_io_map(AIRBED_IO.replace("PIN", "7").replace("vent_or_bag_valve", "bag_vent_valve"))
        changes = self.ic.diff_io_maps(before, after)
        self.assertEqual([(c["kind"], c.get("renamed_from")) for c in changes], [("renamed", "vent_or_bag_valve")])

    def test_readback_write_mismatch_warns(self):
        before = self.ic.parse_io_map(AIRBED_IO.replace("PIN", "7"))
        text = AIRBED_IO.replace("od_bit(PIN)", "od_bit(7)").replace("5, PIN, value", "5, 6, value")
        changes = self.ic.diff_io_maps(before, self.ic.parse_io_map(text))
        self.assertIn("warning", changes[0])

    MANIFOLD_MD = """
## Pneumatic manifolds

| Riser | OD pin | Manifold number | Manifold pin | Ref des | Valve                                |
| ----- | ------ | --------------- | ------------ | ------- | ------------------------------------ |
| 5     | OD4    | 1               | 13           | V17     | vent_or_bag_valve                    |
| 5     | OD5    | 1               | 14           | V17     | vent_or_bag_valve                    |
| 5     | OD6    | 1               | 15           | -       | UNUSED                               |
| 5     | OD7    | 1               | 16           | V20     | desorb_tube_extraction_gas_out_valve |

## Other
| Riser | OD pin |
| 9     | OD1    |
"""

    def test_manifold_table(self):
        rows = sp.parse_manifold_table(self.MANIFOLD_MD)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2], {"riser": 5, "od_pin": 6, "manifold_number": "1", "manifold_pin": "15", "ref_des": "", "valve": ""})

    def test_coil_annotation_flags_move_onto_spare(self):
        before_rows = sp.parse_manifold_table(self.MANIFOLD_MD)
        after_rows = sp.parse_manifold_table(
            self.MANIFOLD_MD.replace("| 15           | -       | UNUSED                              ", "| 15           | V20     | desorb_tube_extraction_gas_out_valve")
            .replace("| 16           | V20     | desorb_tube_extraction_gas_out_valve", "| 16           | -       | UNUSED                              ")
        )
        ch = self.ic.diff_io_maps(
            self.ic.parse_io_map(AIRBED_IO.replace("PIN", "7")), self.ic.parse_io_map(AIRBED_IO.replace("PIN", "6"))
        )[0]
        self.ic.annotate_coils(ch, "panel_station", before_rows, after_rows)
        self.assertIn("now drives riser 5 OD6 = manifold 1 pin 15", ch["coils"])
        self.assertTrue(any("OD7 = manifold 1 pin 16, documented UNUSED" in c for c in ch["coils"]))
        self.assertTrue(any("UNUSED (spare coil) before this commit" in w for w in ch["coil_warnings"]))
        self.assertTrue(any("physically re-plumbed" in w for w in ch["coil_warnings"]))

    def test_double_to_single_is_not_a_move(self):
        text = AIRBED_IO.replace("PIN", "7")
        single = text.replace("Some(!v.od_bit(4)? && v.od_bit(5)?)", "v.od_bit(4)").replace("5, 4, 5, value", "5, 4, value").replace("set_od_pair", "set_od")
        ch = next(c for c in self.ic.diff_io_maps(self.ic.parse_io_map(text), self.ic.parse_io_map(single)) if c["signal"] == "vent_or_bag_valve")
        rows = sp.parse_manifold_table(self.MANIFOLD_MD)
        self.ic.annotate_coils(ch, "panel_station", rows, rows)
        self.assertNotIn("coil_warnings", ch)
        self.assertEqual(ch["coils"], ["no longer drives riser 5 OD5 = manifold 1 pin 14"])

    def test_mark_reverts(self):
        bad = {"sha": "731e0e8", "when": "2026-10-28T20:02:00Z", "io": {"remaps": [{"signal": "s", "before": "OD7", "after": "OD6"}]}}
        fix = {"sha": "9256f09", "when": "2026-10-29T13:57:00Z", "io": {"remaps": [{"signal": "s", "before": "OD6", "after": "OD7"}]}}
        self.ic.mark_reverts([fix, bad])
        self.assertEqual(fix["io"]["remaps"][0]["reverts"], "731e0e8")
        self.assertEqual(bad["io"]["remaps"][0]["reverted_by"]["sha"], "9256f09")

    def test_io_change_log_pairs_restarts_on_deployed_branch(self):
        import change_digest as cd

        commits = [
            {
                "sha": "731e0e8fb",
                "when": "2026-10-28T16:02:17-04:00",
                "authored": "2026-10-28T12:45:43-04:00",
                "author": "Spark",
                "subject": "drive extraction gas out on OD6",
                "branches": ["ix/refactor-automation"],
                "sample_files": ["control/src/system/airbed.rs"],
                "io": {
                    "remaps": [{"signal": "desorb_tube_extraction_gas_out_valve", "risers": ["panel_station:5"],
                                "before": "OD7", "after": "OD6", "file": "control/src/system/airbed.rs"}],
                    "excerpt": [],
                },
            }
        ]
        restarts = {"airbed-host": {"restarts": [{"ts": "2026-10-28 15:00:00+00"}, {"ts": "2026-10-28 21:03:04+00"}]}}
        until = datetime(2026, 10, 29, 12, 30, tzinfo=timezone.utc)
        log = cd.io_change_log(commits, restarts, {"airbed-host": "origin/ix/refactor-automation"}, until)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["restarts_since_authored"], ["2026-10-28 21:03:04+00"])
        self.assertEqual(cd.io_change_log(commits, restarts, {"airbed-host": "origin/main"}, until), [])


try:
    sys.path.insert(0, str(HERE.parents[1] / "container" / "telemetry-mcp"))
    import investigate  # noqa: E402

    HAVE_INVESTIGATE = True
except ImportError:  # duckdb not installed on this interpreter
    HAVE_INVESTIGATE = False


@unittest.skipUnless(HAVE_INVESTIGATE, "needs duckdb (run under uvx --with duckdb --with boto3)")
class ChangeCheckTests(unittest.TestCase):
    def test_stale_input_riser_outranks_newer_remap(self):
        digest = {
            "generated_at": "2026-10-29T12:30:00Z",
            "deployed_refs": {"airbed-host": "origin/ix/refactor-automation"},
            "io_map": {"airbed-host": {"signals": {
                "vent_or_bag_valve": {"station": "panel_station", "riser": 5, "read": [["od_bit", 4]], "write": [4, 5]},
                "extraction_vac_drain_valve": {"station": "panel_station", "riser": 2, "read": [["od_bit", 4]], "write": [4]},
            }}},
            "io_changes": [
                {"host": "airbed-host", "branch": "ix/refactor-automation", "sha": "2b2538e1c", "author": "Spark",
                 "authored": "2026-10-28T20:00:00Z", "subject": "riser 2 change",
                 "remaps": [{"signal": "extraction_vac_vent_valve", "risers": ["panel_station:2"], "before": "OD6+OD7", "after": "OD6"}],
                 "excerpt": [], "restarts_since_authored": ["2026-10-28 21:03:04+00"]},
                {"host": "airbed-host", "branch": "ix/refactor-automation", "sha": "731e0e8fb", "author": "Spark",
                 "authored": "2026-10-28T16:45:00Z", "subject": "drive extraction gas out on OD6",
                 "remaps": [{"signal": "desorb_tube_extraction_gas_out_valve", "risers": ["panel_station:5"], "before": "OD7", "after": "OD6"}],
                 "excerpt": [], "restarts_since_authored": ["2026-10-28 21:03:04+00"]},
            ],
            "restarts": {"airbed-host": {"restarts": [{"ts": "2026-10-28 21:03:04+00"}]}},
            "commits": {"co3ntrol-rs": []},
        }
        onset = datetime(2026, 10, 29, 5, 42, 22, tzinfo=timezone.utc)
        with mock.patch.object(investigate.sigs, "load_digest", return_value=digest):
            out = investigate.changes_before(
                "airbed-host", onset,
                ["airbed.controls.vent_or_bag_valve", "airbed.controls.extraction_vac_drain_valve"],
                primary=["control.vent_or_bag_valve"],
            )
            later = investigate.changes_before("airbed-host", datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc), ["airbed.controls.vent_or_bag_valve"])
        self.assertEqual([i["sha"] for i in out["io_changes_on_affected_risers"]], ["731e0e8fb", "2b2538e1c"])
        self.assertTrue(out["assessment"].startswith("731e0e8fb"))
        self.assertIn("OD7 → OD6", out["assessment"])
        self.assertEqual(out["match"], "same riser", "a station dropout is not the remapped output's symptom")
        with mock.patch.object(investigate.sigs, "load_digest", return_value=digest):
            valve = investigate.changes_before(
                "airbed-host", datetime(2026, 10, 29, 12, 45, tzinfo=timezone.utc), [],
                primary=["desorb_tube_extraction_gas_out_valve"],
            )
        self.assertEqual(valve["match"], "direct")
        self.assertIn("readback still follows the command", valve["assessment"])
        self.assertEqual(later["io_changes_on_affected_risers"], [], "not live before onset: no restart after the commit yet")


if __name__ == "__main__":
    unittest.main()
