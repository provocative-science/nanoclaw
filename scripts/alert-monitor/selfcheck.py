"""Minimal self-check for alert edge helpers (no network)."""

from __future__ import annotations

from conditions import decode_bits, decode_conditions
from prompt import build_prompt


def test_decode_bits() -> None:
    assert decode_bits(0) == []
    assert decode_bits(1 << 3 | 1 << 5) == [3, 5]


def test_decode_conditions() -> None:
    conds = decode_conditions(1 << 4, severity="error")
    assert conds[0]["bit"] == 4
    assert "holdoff" in conds[0]["label"].lower()


def test_build_prompt_includes_alert() -> None:
    alert = {"subsystem": "liquefaction", "kind": "tower_error", "tower": {"error": True}}
    text = build_prompt(alert, alert_id="plant-alert-test-1")
    assert "tower_error" in text
    assert "upstream" in text.lower()
    assert "grafanacloud-prom" in text
    assert "recent_deploy" in text
    assert "plant-alert-test-1" in text
    assert "/workspace/shared/plant-alerts/latest.md" in text
    # Delivery order: archive tools before final brief (empty SDK result drop)
    assert "Stop all tools" in text
    assert "the final brief message" in text


def test_plant_alerts_archive(tmp_path) -> None:
    from plant_alerts_log import plant_alerts_dir, record_alert_event

    root = tmp_path
    alert = {
        "subsystem": "liquefaction",
        "kind": "tower_warning",
        "fired_at": "2026-07-22T10:00:00-04:00",
        "tower": {"warning": True, "error": False},
        "sensors": {
            "system_state": "RUN",
            "active_warnings": 8,
            "o2_ppm": 999,
        },
    }
    result = record_alert_event(
        root, alert_id="plant-alert-unit-1", alert=alert, dry_run=False
    )
    d = plant_alerts_dir(root)
    assert (d / "log.jsonl").is_file()
    assert (d / "latest.md").is_file()
    assert (d / "latest-event.json").is_file()
    log = (d / "log.jsonl").read_text(encoding="utf-8")
    assert "plant-alert-unit-1" in log
    assert "tower_warning" in log
    latest = (d / "latest.md").read_text(encoding="utf-8")
    assert "plant-alert-unit-1" in latest
    assert result["dry_run"] is False


def test_recent_deploy_systick() -> None:
    from recent_deploy import build_recent_deploy, host_restart_check

    block = build_recent_deploy(
        [
            host_restart_check(subsystem="container", systick=10, threshold_s=1800),
            host_restart_check(
                subsystem="liquefaction", systick=99999, threshold_s=1800
            ),
        ],
        threshold_s=1800,
    )
    assert block["likely"] is True


if __name__ == "__main__":
    from pathlib import Path
    import tempfile

    test_decode_bits()
    test_decode_conditions()
    test_build_prompt_includes_alert()
    with tempfile.TemporaryDirectory() as td:
        test_plant_alerts_archive(Path(td))
    test_recent_deploy_systick()
    print("ok")
