"""Advertisement-triggered polling coordinator."""

from __future__ import annotations

import asyncio
import logging
import time

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_POLL_INTERVAL,
    CONNECT_ATTEMPTS,
    DEFAULT_POLL_INTERVAL,
    POLL_TIMEOUT,
    RETRY_INTERVAL,
    STALE_INTERVALS,
    STALE_MIN,
)
from .drivers import CharacteristicMissing, Driver, DriverError, Reading

_LOGGER = logging.getLogger(__name__)


class BatteryMonitorCoordinator(ActiveBluetoothDataUpdateCoordinator[Reading]):
    """Polls one monitor whenever it is heard and a poll is due.

    Each poll connects, reads, and disconnects, so the proxy's connection slot
    is only held for a few seconds.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, driver: Driver) -> None:
        self.entry = entry
        self.driver = driver
        self.last_success: float | None = None  # wall clock of last good reading
        self.last_error: str | None = None
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
    def has_fresh_data(self) -> bool:
        """True while the last good reading is recent enough to show."""
        if self.data is None or self.last_success is None:
            return False
        max_age = max(STALE_MIN, STALE_INTERVALS * self.poll_interval)
        return time.time() - self.last_success < max_age

    @callback
    def _needs_poll(
        self, service_info: bluetooth.BluetoothServiceInfoBleak, seconds_since_poll: float | None
    ) -> bool:
        if self.hass.state is not CoreState.running:
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
