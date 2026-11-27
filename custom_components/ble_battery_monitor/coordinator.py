"""Polling coordinator: advertisement- and timer-triggered connect/read/disconnect."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import time

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHEMISTRY,
    CONF_HISTORY,
    CONF_NOMINAL_VOLTAGE,
    CONF_POLL_INTERVAL,
    CONNECT_ATTEMPTS,
    DEFAULT_CHEMISTRY,
    DEFAULT_HISTORY,
    DEFAULT_NOMINAL_VOLTAGE,
    DEFAULT_POLL_INTERVAL,
    HISTORY_GAP,
    HISTORY_MAX_AGE,
    HISTORY_RETRY,
    HISTORY_TIMEOUT,
    POLL_TIMEOUT,
    POLL_TIMER,
    RETRY_INTERVAL,
    RETRY_MAX,
    STALE_INTERVALS,
    STALE_MIN,
)
from .drivers import CharacteristicMissing, Driver, DriverError, HistorySample, Reading
from .history import async_import_history
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

    Backfill: after a gap between good readings (or a restart), the same
    connection also downloads the device's own log, and the missing hours are
    imported as long-term statistics.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, driver: Driver) -> None:
        self.entry = entry
        self.driver = driver
        driver.logging_enabled = driver.supports_history and bool(
            entry.options.get(CONF_HISTORY, DEFAULT_HISTORY)
        )
        self.last_success: float | None = None  # wall clock of last good reading
        self.last_error: str | None = None
        self._polling = False
        #: Failed polls in a row (drives the retry back-off).
        self.consecutive_failures = 0
        #: Monotonic time of the last connection attempt (adverts are stamped on this clock).
        self._last_attempt: float | None = None
        #: Serializes connections (polls and service calls).
        self.lock = asyncio.Lock()
        self._force_poll = False
        #: Start of a gap that still needs backfilling (None = nothing to do).
        #: Starts out set, so the first poll after startup also covers downtime.
        self._history_since: float | None = time.time() - HISTORY_MAX_AGE
        self._history_attempt: float | None = None
        self.history_status: dict[str, object] = {}
        #: A step after a good download that wasn't acknowledged (shown in diagnostics).
        self._history_warning: str | None = None
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
    def history_enabled(self) -> bool:
        return self.driver.supports_history and bool(
            self.entry.options.get(CONF_HISTORY, DEFAULT_HISTORY)
        )

    @property
    def nominal_voltage(self) -> int:
        return int(self.entry.options.get(CONF_NOMINAL_VOLTAGE, DEFAULT_NOMINAL_VOLTAGE))

    def _history_due(self) -> bool:
        if not self.history_enabled:
            return False
        # A gap to fill, or (KONNWEI) the on-device logging schedule needs renewing.
        if self._history_since is None and not self.driver.history_wanted():
            return False
        return self._history_attempt is None or time.time() - self._history_attempt >= HISTORY_RETRY

    @callback
    def async_request_history(self) -> None:
        """Backfill as far back as the device allows, on the next poll (now)."""
        self._history_since = time.time() - HISTORY_MAX_AGE
        self._history_attempt = None
        self.async_poll_now()

    @callback
    def async_poll_now(self) -> None:
        self._force_poll = True
        self._async_poll_timer(dt_util.utcnow())

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
        if self._force_poll:
            self._force_poll = False
            return True
        if seconds_since_poll is not None and seconds_since_poll < self.retry_due:
            return False
        if (
            self.consecutive_failures
            and self._last_attempt is not None
            and service_info.time <= self._last_attempt
        ):
            # Not heard since the attempt that failed: the timer is only re-feeding the old
            # sighting, and connecting blind just keeps the proxy busy (and not scanning).
            return False
        return (
            bluetooth.async_ble_device_from_address(
                self.hass, service_info.device.address, connectable=True
            )
            is not None
        )

    @property
    def retry_due(self) -> float:
        """Seconds to wait after the last poll: the interval, or the back-off after failures."""
        if not self.consecutive_failures:
            return self.poll_interval
        first = min(self.poll_interval, RETRY_INTERVAL)
        backoff = first * 2 ** min(self.consecutive_failures - 1, 16)
        return min(backoff, max(RETRY_MAX, self.poll_interval))

    def _ble_device_callback(self, fallback: BLEDevice) -> Callable[[], BLEDevice]:
        """Fresh best path for each connect attempt.

        HA picks the proxy by signal strength and penalises one after failed connects, but only
        when asked again. Without this, every retry goes back to the proxy that just failed.
        """

        def _best() -> BLEDevice:
            return (
                bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
                or fallback
            )

        return _best

    async def _establish(self, ble_device: BLEDevice) -> BleakClientWithServiceCache:
        return await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.name or ble_device.address,
            max_attempts=CONNECT_ATTEMPTS,
            ble_device_callback=self._ble_device_callback(ble_device),
        )

    async def _poll(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> Reading:
        self._polling = True
        try:
            async with self.lock:
                return await self._poll_once(service_info)
        finally:
            self._polling = False

    async def async_connect(self) -> BleakClientWithServiceCache:
        """Connect to the monitor (caller disconnects; hold self.lock)."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise BleakError("not currently reachable by any connectable scanner")
        return await self._establish(ble_device)

    async def _poll_once(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> Reading:
        ble_device = (
            bluetooth.async_ble_device_from_address(
                self.hass, service_info.device.address, connectable=True
            )
            or service_info.device
        )
        self._last_attempt = monotonic_time_coarse()
        try:
            reading = await self._poll_connection(ble_device)
        except BaseException:
            self.consecutive_failures += 1
            raise
        self.consecutive_failures = 0
        return reading

    async def _poll_connection(self, ble_device: BLEDevice) -> Reading:
        now = time.time()
        if self.last_success is not None and now - self.last_success > HISTORY_GAP:
            gap_start = self.last_success
            self._history_since = min(self._history_since or gap_start, gap_start)
        samples: list[HistorySample] | None = None
        client: BleakClientWithServiceCache | None = None
        try:
            try:
                async with asyncio.timeout(POLL_TIMEOUT):
                    client = await self._establish(ble_device)
                    try:
                        reading = await self.driver.async_read(client)
                    except CharacteristicMissing:
                        await client.clear_cache()
                        raise
                if self._history_due():
                    samples = await self._read_history(client)
            finally:
                if client is not None:
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
                reading.voltage, self.chemistry, self.nominal_voltage, reading.temperature
            )
        self.last_success = time.time()
        self.last_error = None
        self._async_update_device_registry()
        if samples is not None:
            await self._async_import(samples)
        return reading

    async def _read_history(
        self, client: BleakClientWithServiceCache
    ) -> list[HistorySample] | None:
        """Download the device log; failures are logged and retried later, not raised."""
        since = self._history_since or (time.time() - HISTORY_MAX_AGE)
        self._history_attempt = time.time()
        try:
            async with asyncio.timeout(HISTORY_TIMEOUT):
                samples = await self.driver.async_read_history(client, since)
        except (TimeoutError, BleakError, DriverError) as err:
            msg = "timeout" if isinstance(err, TimeoutError) else f"{type(err).__name__}: {err}"
            self.history_status = {"at": round(time.time()), "error": msg}
            _LOGGER.warning("%s: history download failed (%s); will retry", self.address, msg)
            return None
        self._history_since = None  # the gap is covered (the samples are in hand)
        if error := (self.driver.history_info or {}).get("error"):
            # Downloaded, but a later step (clearing the device log, restarting its
            # schedule) wasn't acknowledged. Keep the samples, and redo that step after
            # the usual back-off instead of on the very next poll.
            self._history_warning = error
            _LOGGER.info("%s: history downloaded, but %s; will retry", self.address, error)
        else:
            self._history_warning = None
            self._history_attempt = None  # the retry back-off only follows failures
        return samples

    async def _async_import(self, samples: list[HistorySample]) -> None:
        values: dict[str, Callable[[HistorySample], float | None]] = {}
        sensors = self.driver.sensors
        if "voltage" in sensors:
            values["voltage"] = lambda s: s.voltage
        if "soc" in sensors:
            values["soc"] = lambda s: s.soc
        if "temperature" in sensors:
            values["temperature"] = lambda s: s.temperature
        if self.estimates_soc:
            chemistry, nominal = self.chemistry, self.nominal_voltage
            values["soc_estimate"] = lambda s: estimate_soc(
                s.voltage, chemistry, nominal, s.temperature
            )
        status: dict[str, object] = {
            "at": round(time.time()),
            "samples": len(samples),
            "oldest": round(samples[0].ts) if samples else None,
            "newest": round(samples[-1].ts) if samples else None,
        }
        if self._history_warning:
            status["warning"] = self._history_warning
        try:
            status["imported_hours"] = await async_import_history(
                self.hass, self.address, samples, values
            )
        except Exception as err:
            _LOGGER.exception("%s: importing history failed", self.address)
            status["error"] = f"{type(err).__name__}: {err}"
        self.history_status = status
        _LOGGER.info("%s: history backfill %s", self.address, status)

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
