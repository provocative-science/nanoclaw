"""Offline edge-detection tests (no plant network)."""

from __future__ import annotations

from pathlib import Path

from monitor import AlertMonitor
from loki_abort import AbortLogHit, line_is_abort, new_hits_after, parse_loki_streams


class CaptureMonitor(AlertMonitor):
    def __init__(self) -> None:
        super().__init__(
            liq_base="http://liq.test",
            bop_base="http://bop.test",
            liq_token="",
            bop_token="",
            nanoclaw_root=Path("/tmp"),
            ipc_group_folder="telegram_main",
            target_jid="tg:test",
            cooldown_s=0,
            poll_interval_s=1,
            dry_run=True,
        )
        self.fired: list[dict] = []

    def fire(self, alert, cooldown_keys):  # type: ignore[override]
        from mute_state import is_muted

        if is_muted(self.nanoclaw_root):
            return
        now = __import__("time").time()
        if not all(self.cooldown.allow(k, now) for k in cooldown_keys):
            return
        self.fired.append({"alert": alert, "keys": list(cooldown_keys)})
        for k in cooldown_keys:
            self.cooldown.mark(k, now)


def test_liq_tower_and_new_bits() -> None:
    m = CaptureMonitor()
    # prime
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": False,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": 0,
                    "active_errors": 0,
                },
            }
        }
    )
    assert m.fired == []

    # warning tower on with bit 3
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": True,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": 1 << 3,
                    "active_errors": 0,
                },
            }
        }
    )
    assert len(m.fired) == 1
    assert m.fired[0]["alert"]["kind"] == "tower_warning"

    # new bit while light still on
    m.fired.clear()
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": True,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": (1 << 3) | (1 << 5),
                    "active_errors": 0,
                },
            }
        }
    )
    assert len(m.fired) == 1
    assert m.fired[0]["alert"]["kind"] == "new_warning_bits"
    assert m.fired[0]["alert"]["new_conditions"][0]["bit"] == 5


def test_liq_system_state_error_edge() -> None:
    """RUN → ERROR must fire even when tower light / HAZOP bits stay clear."""
    m = CaptureMonitor()
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": False,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": 0,
                    "active_errors": 0,
                },
            }
        }
    )
    assert m.fired == []

    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": False,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "ERROR",
                    "system_state_code": 3,
                    "active_warnings": 0,
                    "active_errors": 0,
                },
            }
        }
    )
    assert len(m.fired) == 1
    alert = m.fired[0]["alert"]
    assert alert["kind"] == "system_state_error"
    assert alert["system_state"] == "ERROR"
    assert alert["previous_system_state"] == "RUN"
    assert "liquefaction:system_state_error" in m.fired[0]["keys"]

    # Sticky ERROR — no re-fire
    m.fired.clear()
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": False,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "ERROR",
                    "system_state_code": 3,
                    "active_warnings": 0,
                    "active_errors": 0,
                },
            }
        }
    )
    assert m.fired == []


def test_liq_state_error_with_tower_same_poll() -> None:
    """State ERROR + tower error in one poll → single combined alert."""
    m = CaptureMonitor()
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": False,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": 0,
                    "active_errors": 0,
                },
            }
        }
    )
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": False,
                    "tower_error_indicator": True,
                },
                "sensors": {
                    "system_state": "ERROR",
                    "system_state_code": 3,
                    "active_warnings": 0,
                    "active_errors": 0,
                },
            }
        }
    )
    assert len(m.fired) == 1
    assert m.fired[0]["alert"]["kind"] == "tower_error+system_state_error"
    assert m.fired[0]["alert"]["previous_system_state"] == "RUN"
    keys = m.fired[0]["keys"]
    assert "liquefaction:tower_error" in keys
    assert "liquefaction:system_state_error" in keys


def test_liq_suppress_warnings_in_debug() -> None:
    m = CaptureMonitor()
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": False,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "DEBUG",
                    "system_state_code": 2,
                    "active_warnings": 0,
                    "active_errors": 0,
                },
            }
        }
    )

    # warning while DEBUG — suppressed
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": True,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "DEBUG",
                    "system_state_code": 2,
                    "active_warnings": 1 << 3,
                    "active_errors": 0,
                },
            }
        }
    )
    assert m.fired == []

    # error while DEBUG — still fires
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": True,
                    "tower_error_indicator": True,
                },
                "sensors": {
                    "system_state": "DEBUG",
                    "system_state_code": 2,
                    "active_warnings": 1 << 3,
                    "active_errors": 1 << 6,
                },
            }
        }
    )
    assert len(m.fired) == 1
    assert m.fired[0]["alert"]["kind"] == "tower_error"

    # leave DEBUG with warning still lit — no rising edge, still no warn fire
    m.fired.clear()
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": True,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": 1 << 3,
                    "active_errors": 0,
                },
            }
        }
    )
    assert m.fired == []


def test_container_skip_interlock_while_locked() -> None:
    m = CaptureMonitor()
    sample = {
        "app": {
            "supervisor": {
                "lock": False,
                "active_recovery": None,
                "triggered_interlocks": [],
            },
            "system": {"bop": {"sensors": {}}},
        }
    }
    m.process_container(sample)

    # lock rises
    sample["app"]["supervisor"]["lock"] = True
    m.process_container(sample)
    assert m.fired[-1]["alert"]["kind"] == "supervisor_lock"

    # later interlock while still locked — should NOT fire
    n = len(m.fired)
    sample["app"]["supervisor"]["triggered_interlocks"] = [
        {"name": "hot_tank_level_interlock", "msg": "too low"}
    ]
    m.process_container(sample)
    assert len(m.fired) == n

    # unlock then interlock
    sample["app"]["supervisor"]["lock"] = False
    sample["app"]["supervisor"]["triggered_interlocks"] = []
    m.process_container(sample)
    sample["app"]["supervisor"]["triggered_interlocks"] = [
        {"name": "filter_doors_interlock", "msg": "open"}
    ]
    m.process_container(sample)
    assert m.fired[-1]["alert"]["kind"] == "triggered_interlock"


def test_same_poll_lock_and_interlock() -> None:
    m = CaptureMonitor()
    m.process_container(
        {
            "app": {
                "supervisor": {
                    "lock": False,
                    "triggered_interlocks": [],
                },
                "system": {"bop": {"sensors": {}}},
            }
        }
    )
    m.process_container(
        {
            "app": {
                "supervisor": {
                    "lock": True,
                    "triggered_interlocks": [
                        {"name": "extraction_bag_interlock", "msg": "x"}
                    ],
                },
                "system": {"bop": {"sensors": {}}},
            }
        }
    )
    assert m.fired[-1]["alert"]["kind"] == "supervisor_lock_and_interlock"


def test_container_phase_in_alert() -> None:
    from container_phase import infer_container_phases, phase_summary

    system = {
        "dacs": {
            "a": {
                "controls": {"purge_vent_valve": False, "fan_enable": True},
                "filters": {
                    "e": {
                        "controls": {
                            "doors": False,
                            "extraction_valve": True,
                            "purge_valve": False,
                            "inlet_hot_valve": True,
                            "inlet_warm_valve": False,
                        }
                    },
                    "b": {
                        "controls": {
                            "doors": False,
                            "extraction_valve": False,
                            "purge_valve": True,
                            "inlet_hot_valve": False,
                            "inlet_warm_valve": False,
                        }
                    },
                },
            }
        },
        "bop": {"controls": {"purge_vacuum_enable": True}},
    }
    phases = infer_container_phases(system)
    assert phases == ["Extracting Filter E", "Purging Filter B"]
    assert phase_summary(system) == "Extracting Filter E; Purging Filter B"

    venting = {
        "dacs": {
            "a": {
                "controls": {"purge_vent_valve": True},
                "filters": {
                    "b": {
                        "controls": {
                            "doors": False,
                            "extraction_valve": False,
                            "purge_valve": True,
                            "inlet_hot_valve": False,
                            "inlet_warm_valve": False,
                        }
                    }
                },
            }
        },
        "bop": {"controls": {}},
    }
    assert infer_container_phases(venting) == ["Venting Filter B"]

    m = CaptureMonitor()
    m.process_container(
        {
            "app": {
                "supervisor": {"lock": False, "triggered_interlocks": []},
                "system": {"bop": {"sensors": {}}, "dacs": {}},
            }
        }
    )
    m.process_container(
        {
            "app": {
                "supervisor": {"lock": True, "triggered_interlocks": []},
                "system": system,
            }
        }
    )
    assert m.fired[-1]["alert"]["phase"] == "Extracting Filter E; Purging Filter B"
    assert "filter_controls" in m.fired[-1]["alert"]


def test_mute_skips_fire(tmp_path: Path) -> None:
    from mute_state import parse_mute_command, write_mute_state

    assert parse_mute_command("@Ghost mute alerts") == "mute"
    assert parse_mute_command("@Ghost unmute alerts please") == "unmute"
    assert parse_mute_command("@Ghost mute alerts\nthanks") == "mute"
    assert parse_mute_command("hello") is None

    m = CaptureMonitor()
    m.nanoclaw_root = tmp_path
    write_mute_state(tmp_path, muted=True, command="mute")

    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": False,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": 0,
                    "active_errors": 0,
                },
            }
        }
    )
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": True,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": 1 << 3,
                    "active_errors": 0,
                },
            }
        }
    )
    assert m.fired == []

    write_mute_state(tmp_path, muted=False, command="unmute")
    m.process_liquefaction(
        {
            "liquefaction": {
                "controls": {
                    "tower_warning_indicator": True,
                    "tower_error_indicator": False,
                },
                "sensors": {
                    "system_state": "RUN",
                    "system_state_code": 1,
                    "active_warnings": (1 << 3) | (1 << 5),
                    "active_errors": 0,
                },
            }
        }
    )
    assert len(m.fired) == 1
    assert m.fired[0]["alert"]["kind"] == "new_warning_bits"


def test_recent_deploy_helpers() -> None:
    from recent_deploy import (
        build_recent_deploy,
        extract_systick,
        host_restart_check,
    )

    assert extract_systick({"app": {"systick": 120}}) == 120
    assert extract_systick({"systick": 5}) == 5
    assert extract_systick({}) is None

    recent = host_restart_check(
        subsystem="container", systick=60, threshold_s=1800
    )
    assert recent["recent"] is True
    stale = host_restart_check(
        subsystem="liquefaction", systick=10000, threshold_s=1800
    )
    assert stale["recent"] is False

    block = build_recent_deploy([recent, stale], threshold_s=1800)
    assert block["likely"] is True
    assert block["checks"][0]["subsystem"] == "container"




def test_loki_line_match_helpers() -> None:
    assert line_is_abort("Purge of filter a failed! Aborting.")
    assert line_is_abort("Vent of filter b failed!")
    assert line_is_abort("Exiting for health reasons")
    assert line_is_abort("Control set failed (bop.x); aborting.")
    assert not line_is_abort("Begin filter a")
    assert not line_is_abort("response failed: unknown, trying again in 1s")
    assert not line_is_abort(
        "2026-07-23T02:13:35Z INFO alloy-deploy-smoke co3ntrol-automation"
    )


def test_parse_loki_streams_filters() -> None:
    payload = {
        "data": {
            "result": [
                {
                    "values": [
                        ["100", "Begin filter a"],
                        ["200", "Purge of filter a failed! Aborting."],
                        ["150", "Setting bop control x to True"],
                    ]
                }
            ]
        }
    }
    hits = parse_loki_streams(payload)
    assert [h.ts_ns for h in hits] == [200]
    assert new_hits_after(hits, 200) == []
    assert len(new_hits_after(hits, 199)) == 1


def test_loki_automation_abort_edge() -> None:
    m = CaptureMonitor()

    class FakeLoki:
        enabled = True

        def __init__(self) -> None:
            self.calls = 0

        def fetch_abort_hits(self, *, now=None):
            self.calls += 1
            if self.calls == 1:
                return [
                    AbortLogHit(
                        ts_ns=200, line="Purge of filter a failed! Aborting."
                    )
                ]
            if self.calls == 2:
                return [
                    AbortLogHit(
                        ts_ns=200, line="Purge of filter a failed! Aborting."
                    ),
                    AbortLogHit(ts_ns=300, line="Vent of filter b failed!"),
                ]
            return [
                AbortLogHit(ts_ns=300, line="Vent of filter b failed!"),
                AbortLogHit(ts_ns=400, line="Exiting for health reasons"),
            ]

    m.loki = FakeLoki()
    m.loki_poll_interval_s = 0

    m.process_automation_loki(now=1.0)
    assert m.fired == []
    assert m.state.loki_primed is True
    assert m.state.loki_last_ts_ns == 200

    m.process_automation_loki(now=2.0)
    assert len(m.fired) == 1
    assert m.fired[0]["alert"]["kind"] == "job_failed"
    assert m.fired[0]["alert"]["matched_line"] == "Vent of filter b failed!"
    assert m.fired[0]["keys"] == ["container:job_failed:log"]

    m.fired.clear()
    m.process_automation_loki(now=3.0)
    assert len(m.fired) == 1
    assert m.fired[0]["alert"]["matched_line"] == "Exiting for health reasons"

if __name__ == "__main__":
    from pathlib import Path
    import tempfile

    test_liq_tower_and_new_bits()
    test_liq_system_state_error_edge()
    test_liq_state_error_with_tower_same_poll()
    test_liq_suppress_warnings_in_debug()
    test_container_skip_interlock_while_locked()
    test_same_poll_lock_and_interlock()
    test_container_phase_in_alert()
    test_loki_line_match_helpers()
    test_parse_loki_streams_filters()
    test_loki_automation_abort_edge()
    with tempfile.TemporaryDirectory() as td:
        test_mute_skips_fire(Path(td))
    test_recent_deploy_helpers()
    print("ok")
