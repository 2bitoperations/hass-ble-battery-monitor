"""SoC estimate from resting voltage."""

import pytest

from custom_components.ble_battery_monitor.soc import CHEMISTRIES, estimate_soc


@pytest.mark.parametrize(
    ("volts", "chem", "expected"),
    [
        (12.73, "flooded", 100),
        (13.40, "flooded", 100),  # charging / surface charge clamps
        (12.10, "flooded", 50),
        (12.17, "flooded", 55),
        (11.51, "flooded", 10),
        (11.21, "flooded", 0),
        (10.00, "flooded", 0),
        (12.24, "agm", 50),
        (13.10, "lifepo4", 50),
    ],
)
def test_table_points(volts, chem, expected):
    assert estimate_soc(volts, chem, 12, 25.0) == expected


def test_scaling_and_none():
    assert estimate_soc(24.20, "flooded", 24, 25.0) == 50
    assert estimate_soc(6.05, "flooded", 6, 25.0) == 50
    assert estimate_soc(12.1, "none") is None
    assert estimate_soc(None, "flooded") is None


def test_cold_battery_corrected_up_slightly():
    warm = estimate_soc(12.10, "flooded", 12, 25.0)
    cold = estimate_soc(12.10, "flooded", 12, -5.0)
    assert warm == 50 and 51 <= cold <= 54  # ~1 % per 10 C, not a big swing
    assert estimate_soc(13.10, "lifepo4", 12, -5.0) == 50  # no correction for LFP


def test_monotonic():
    for chem in CHEMISTRIES[:-1]:
        values = [estimate_soc(10 + i / 100, chem, 12, 25.0) for i in range(0, 400)]
        assert values == sorted(values)
