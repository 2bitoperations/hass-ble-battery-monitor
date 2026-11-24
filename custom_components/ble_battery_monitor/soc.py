"""State-of-charge estimate from resting (open-circuit) voltage.

Only meaningful when the battery has rested (no charge or load) for a few hours;
while charging or under load the estimate reads high or low respectively.
Used for monitors that don't report SoC themselves (KONNWEI BK series).

Tables are for a 12 V battery at 25 C and are scaled for other nominal voltages
(6 V = 3 cells ... 48 V = 24 cells). Values are typical published resting-voltage
charts (e.g. Trojan for flooded); individual batteries vary by a few percent.

Temperature: lead-acid open-circuit voltage rises about 0.2 mV/C per cell
(~1.2 mV/C for 12 V), i.e. roughly 1 % SoC per 10 C, so a cold battery reads
slightly low. That small term is corrected; it is NOT the much larger
-3..-5 mV/C/cell coefficient used for charging-voltage compensation.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Final

CHEM_NONE: Final = "none"
CHEM_FLOODED: Final = "flooded"
CHEM_AGM: Final = "agm"
CHEM_GEL: Final = "gel"
CHEM_LIFEPO4: Final = "lifepo4"

CHEMISTRIES: Final = (CHEM_FLOODED, CHEM_AGM, CHEM_GEL, CHEM_LIFEPO4, CHEM_NONE)
NOMINAL_VOLTAGES: Final = (6, 12, 24, 36, 48)

# (volts at 12 V nominal, percent), ascending.
_TABLES: Final[dict[str, tuple[tuple[float, int], ...]]] = {
    CHEM_FLOODED: (
        (11.51, 10),
        (11.66, 20),
        (11.81, 30),
        (11.96, 40),
        (12.10, 50),
        (12.24, 60),
        (12.37, 70),
        (12.50, 80),
        (12.62, 90),
        (12.73, 100),
    ),
    CHEM_AGM: (
        (11.66, 10),
        (11.81, 20),
        (11.96, 30),
        (12.10, 40),
        (12.24, 50),
        (12.37, 60),
        (12.50, 70),
        (12.62, 80),
        (12.74, 90),
        (12.85, 100),
    ),
    CHEM_GEL: (
        (11.80, 10),
        (11.95, 20),
        (12.05, 30),
        (12.15, 40),
        (12.25, 50),
        (12.40, 60),
        (12.50, 70),
        (12.65, 80),
        (12.75, 90),
        (12.85, 100),
    ),
    # 4S LiFePO4 at rest. The curve is very flat between ~20 and 90 %, so treat
    # this as a coarse indicator only.
    CHEM_LIFEPO4: (
        (12.00, 10),
        (12.80, 20),
        (13.00, 30),
        (13.05, 40),
        (13.10, 50),
        (13.15, 60),
        (13.20, 70),
        (13.25, 80),
        (13.30, 90),
        (13.40, 100),
    ),
}
# The bottom of each table maps to 10 %; below it, extrapolate to 0 % over this span.
_EMPTY_SPAN: Final = 0.3

_LEAD_ACID: Final = {CHEM_FLOODED, CHEM_AGM, CHEM_GEL}
_TEMP_COEFF_12V: Final = 0.0012  # V per C, lead-acid open-circuit voltage
_REF_TEMP: Final = 25.0


def estimate_soc(
    voltage: float | None,
    chemistry: str,
    nominal: int = 12,
    temperature: float | None = None,
) -> int | None:
    """Return an estimated SoC (0-100) or None if not applicable."""
    table = _TABLES.get(chemistry)
    if voltage is None or table is None or voltage <= 0:
        return None
    v = voltage * 12.0 / nominal
    if temperature is not None and chemistry in _LEAD_ACID:
        v += _TEMP_COEFF_12V * (_REF_TEMP - temperature)
    if v >= table[-1][0]:
        return 100
    lo_v, lo_p = table[0]
    if v < lo_v:
        return max(0, round(lo_p * (v - (lo_v - _EMPTY_SPAN)) / _EMPTY_SPAN))
    for (v0, p0), (v1, p1) in pairwise(table):
        if v0 <= v < v1:
            return round(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return None  # unreachable
