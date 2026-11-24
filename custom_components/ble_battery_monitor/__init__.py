"""BLE Battery Monitor: KONNWEI BK-series and Leagend BM6 12 V battery monitors."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_DRIVER, CONF_MODEL
from .coordinator import BatteryMonitorCoordinator
from .drivers import DRIVERS

PLATFORMS: list[Platform] = [Platform.SENSOR]

type BatteryMonitorConfigEntry = ConfigEntry[BatteryMonitorCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: BatteryMonitorConfigEntry) -> bool:
    """Set up a monitor from a config entry."""
    driver = DRIVERS[entry.data[CONF_DRIVER]](entry.data[CONF_MODEL])
    coordinator = BatteryMonitorCoordinator(hass, entry, driver)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Start listening last, so entities exist before the first poll lands.
    entry.async_on_unload(coordinator.async_start())
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BatteryMonitorConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: BatteryMonitorConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
