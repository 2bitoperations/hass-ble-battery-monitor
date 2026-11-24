"""Diagnostics: last reading, poll status and recent raw BLE traffic."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from . import BatteryMonitorConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BatteryMonitorConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    driver = coordinator.driver
    info = coordinator._last_service_info
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "details": asdict(driver.details),
        "device_info": getattr(driver, "info", None),
        "reading": asdict(coordinator.data) if coordinator.data else None,
        "last_success": coordinator.last_success,
        "last_poll_successful": coordinator.last_poll_successful,
        "last_error": coordinator.last_error,
        "advertisement": None
        if info is None
        else {
            "name": info.name,
            "rssi": info.rssi,
            "source": info.source,
            "manufacturer_data": {k: v.hex() for k, v in info.manufacturer_data.items()},
            "service_uuids": info.service_uuids,
        },
        "trace": list(driver.trace),
    }
