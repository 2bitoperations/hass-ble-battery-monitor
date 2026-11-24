"""Polling coordinator: advertisement- and timer-triggered connect/read/disconnect."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import time

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_CHEMISTRY,
    CONF_NOMINAL_VOLTAGE,
    CONF_POLL_INTERVAL,
    CONNECT_ATTEMPTS,
    DEFAULT_CHEMISTRY,
    DEFAULT_NOMINAL_VOLTAGE,
    DEFAULT_POLL_INTERVAL,
    POLL_TIMEOUT,
    POLL_TIMER,
    RETRY_INTERVAL,
    STALE_INTERVALS,
    STALE_MIN,
)
from .drivers import CharacteristicMissing, Driver, DriverError, Reading
from .soc import CHEM_NONE, estimate_soc

_LOGGER = logging.getLogger(__name__)


class BatteryMonitorCoordinator(ActiveBluetoothDataUpdateCoordinator[Reading]):
    """Polls one monitor when a poll is due and it has been heard since the last one.

    Each poll connects, reads, and disconnects, so the proxy's connection slot
    is only held for a few seconds.

    HA only dispatches Bluetooth callbacks when advertisement *content* changes,
    and these monitors advertise static data, so adverts alone would trigger one
    poll and then nothing. A timer re-feeds HA's latest sighting (which is
    refreshed on every advert) through the same path.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, driver: Driver) -> None:
        self.entry = entry
        self.driver = driver
        self.last_success: float | None = None  # wall clock of last good reading
        self.last_error: str | None = None
        self._polling = False
        super().__init__(
            hass,
            _LOGGER,
            address=entry.unique_id,  # type: ignore[arg-type]
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
            needs_poll_method=self._needs_poll,
            poll_method=self._poll,
            connectable=True,
        )

    @property
    def poll_interval(self) -> int:
        return int(self.entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))

    @property
    def estimates_soc(self) -> bool:
        """True if SoC is estimated from voltage (device doesn't report it)."""
        return "soc" not in self.driver.sensors and self.chemistry != CHEM_NONE

    @property
    def chemistry(self) -> str:
        return str(self.entry.options.get(CONF_CHEMISTRY, DEFAULT_CHEMISTRY))

    @property
    def has_fresh_data(self) -> bool:
        """True while the last good reading is recent enough to show."""
        if self.data is None or self.last_success is None:
            return False
        max_age = max(STALE_MIN, STALE_INTERVALS * self.poll_interval)
        return time.time() - self.last_success < max_age

    @callback
    def async_start_poll_timer(self) -> CALLBACK_TYPE:
        """Start the periodic poll check; returns the cancel callback."""
        return async_track_time_interval(
            self.hass,
            self._async_poll_timer,
            timedelta(seconds=POLL_TIMER),
            name=f"ble_battery_monitor poll {self.address}",
        )

    @callback
    def _async_poll_timer(self, _now: datetime) -> None:
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        if info is not None:
            self._async_handle_bluetooth_event(info, bluetooth.BluetoothChange.ADVERTISEMENT)

    @callback
    def _needs_poll(
        self, service_info: bluetooth.BluetoothServiceInfoBleak, seconds_since_poll: float | None
    ) -> bool:
        if self.hass.state is not CoreState.running or self._polling:
            return False
        due = (
            self.poll_interval
            if self.last_poll_successful
            else min(self.poll_interval, RETRY_INTERVAL)
        )
        if seconds_since_poll is not None and seconds_since_poll < due:
            return False
        return (
            bluetooth.async_ble_device_from_address(
                self.hass, service_info.device.address, connectable=True
            )
            is not None
        )

    async def _poll(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> Reading:
        self._polling = True
        try:
            return await self._poll_once(service_info)
        finally:
            self._polling = False

    async def _poll_once(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> Reading:
        ble_device = (
            bluetooth.async_ble_device_from_address(
                self.hass, service_info.device.address, connectable=True
            )
            or service_info.device
        )
        try:
            async with asyncio.timeout(POLL_TIMEOUT):
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    ble_device.name or ble_device.address,
                    max_attempts=CONNECT_ATTEMPTS,
                )
                try:
                    reading = await self.driver.async_read(client)
                except CharacteristicMissing:
                    await client.clear_cache()
                    raise
                finally:
                    await client.disconnect()
        except TimeoutError as err:
            self.last_error = "timeout"
            raise BleakError(f"poll timed out after {POLL_TIMEOUT}s") from err
        except (BleakError, DriverError) as err:
            # Expected radio/protocol trouble: the base class logs BleakError as a
            # single line (once, until it recovers) instead of a traceback.
            self.last_error = f"{type(err).__name__}: {err}"
            raise BleakError(self.last_error) from err
        except Exception as err:
            self.last_error = f"{type(err).__name__}: {err}"
            raise
        if self.estimates_soc:
            reading.extra["soc_estimate"] = estimate_soc(
                reading.voltage,
                self.chemistry,
                int(self.entry.options.get(CONF_NOMINAL_VOLTAGE, DEFAULT_NOMINAL_VOLTAGE)),
                reading.temperature,
            )
        self.last_success = time.time()
        self.last_error = None
        self._async_update_device_registry()
        return reading

    @callback
    def _async_update_device_registry(self) -> None:
        details = self.driver.details
        registry = dr.async_get(self.hass)
        # This entry owns exactly one device (all entities share its DeviceInfo).
        devices = dr.async_entries_for_config_entry(registry, self.entry.entry_id)
        if not devices:
            return
        device = devices[0]
        changes = {
            k: v
            for k, v in (
                ("model", details.model),
                ("sw_version", details.sw_version),
                ("hw_version", details.hw_version),
            )
            if v and getattr(device, k) != v
        }
        if changes:
            registry.async_update_device(device.id, **changes)
