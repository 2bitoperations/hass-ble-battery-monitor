"""Monitor family drivers."""

from __future__ import annotations

from .base import Advertisement, CharacteristicMissing, Driver, DriverError, Reading
from .konnwei import KonnweiDriver
from .leagend import LeagendDriver

DRIVERS: dict[str, type[Driver]] = {d.key: d for d in (KonnweiDriver, LeagendDriver)}


def detect(adv: Advertisement) -> tuple[type[Driver], str] | None:
    """Return (driver class, model) for a supported advertisement."""
    for driver in DRIVERS.values():
        if (model := driver.detect(adv)) is not None:
            return driver, model
    return None


__all__ = [
    "DRIVERS",
    "Advertisement",
    "CharacteristicMissing",
    "Driver",
    "DriverError",
    "Reading",
    "detect",
]
