"""HAZOP / liquefaction condition bit labels (from co3ntrol-gui liqConditions.ts)."""

from __future__ import annotations

CONDITION_LABELS: dict[int, str] = {
    # Warnings
    2: "Prefilter dewpoint high",
    3: "Column pressure low (vent forced to zero)",
    5: "Column pressure high (Haskell inhibited)",
    8: "-20°C coolant temperature too low",
    9: "5°C coolant temperature too low",
    10: "-20°C coolant temperature too high",
    11: "5°C coolant temperature too high",
    12: "Vent flow below command",
    13: "Vent flow above command",
    14: "Vent temperature too low",
    15: "Vent temperature too high",
    18: "High level probe submerged too long",
    19: "Low level probe dry",
    22: "Holding tank full (Haskell/vent inhibited)",
    # Errors
    4: "Column pressure low (holdoff exceeded)",
    6: "Column pressure high",
    20: "Low level probe dry (holdoff exceeded)",
    28: "Base controller fault",
    29: "Flow controller fault",
    30: "Vent PID fault",
    31: "Stale critical sensor fault",
}

ERROR_BITS = frozenset({4, 6, 20, 28, 29, 30, 31})


def decode_bits(mask: int | None) -> list[int]:
    if not mask:
        return []
    return [i for i in range(32) if mask & (1 << i)]


def condition_label(bit: int) -> str:
    return CONDITION_LABELS.get(bit, f"Unknown condition ({bit})")


def decode_conditions(mask: int | None, *, severity: str) -> list[dict]:
    out = []
    for bit in decode_bits(mask):
        out.append(
            {
                "bit": bit,
                "label": condition_label(bit),
                "severity": severity,
            }
        )
    return out
