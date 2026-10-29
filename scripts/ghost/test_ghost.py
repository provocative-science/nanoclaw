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


if __name__ == "__main__":
    unittest.main()
