"""Backfill: device log -> recorder long-term statistics, through Home Assistant."""

from __future__ import annotations

from datetime import timedelta
import time
from unittest.mock import AsyncMock, patch

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ble_battery_monitor.const import (
    CONF_DRIVER,
    CONF_MODEL,
    DOMAIN,
    HISTORY_GAP,
)
from custom_components.ble_battery_monitor.drivers import leagend

from .fakes import bm2_device
from .test_integration import BM2, _setup_and_poll


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Override conftest's: the recorder must be set up before hass is created."""
    return


@pytest.fixture(autouse=True)
def _env(auto_enable_custom_integrations, mock_bluetooth, monkeypatch):
    monkeypatch.setattr(leagend, "HISTORY_IDLE", 0.3)
    monkeypatch.setattr(leagend, "BM2_HISTORY_PAUSE", 0.0)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=BM2.address,
        title="Tractor",
        data={CONF_ADDRESS: BM2.address, CONF_DRIVER: "leagend", CONF_MODEL: "BM2"},
    )


async def _voltage_stats(hass: HomeAssistant, since):
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        since,
        None,
        {"sensor.tractor_voltage", "sensor.tractor_state_of_charge"},
        "hour",
        None,
        {"mean", "min", "max"},
    )
    return stats


async def test_bm2_backfill_imports_only_missing_hours(hass: HomeAssistant) -> None:
    """First poll after startup downloads the log; gap hours become statistics."""
    # 6 hours of log at 2-minute steps, newest = now; 12.00 V rising 0.01 V per record
    volts = [round(12.0 + i / 100, 2) for i in range(180)]
    entry = _entry()
    now = dt_util.utcnow()
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    kept = this_hour - timedelta(hours=2)
    # Pretend live data already produced statistics for one of those hours.
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{BM2.address}_voltage", suggested_object_id="tractor_voltage"
    )
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.tractor_voltage",
            "unit_class": "voltage",
            "unit_of_measurement": "V",
        },
        [StatisticData(start=kept, mean=99.0, min=99.0, max=99.0)],
    )
    await async_wait_recording_done(hass)

    coordinator = await _setup_and_poll(hass, entry, BM2, bm2_device(history=volts, interval=0.05))
    assert coordinator.last_poll_successful, coordinator.last_error
    await async_wait_recording_done(hass)
    status = coordinator.history_status
    assert status["samples"] == 180, status
    assert coordinator._history_since is None  # nothing left to backfill

    stats = await _voltage_stats(hass, this_hour - timedelta(hours=8))
    rows = {dt_util.utc_from_timestamp(r["start"]): r for r in stats["sensor.tractor_voltage"]}
    # The current hour is left to live recording; the pre-existing hour is untouched.
    assert this_hour not in rows
    assert rows[kept]["mean"] == 99.0
    imported = [h for h in rows if h != kept]
    assert len(imported) >= 4
    assert status["imported_hours"]["voltage"] == len(imported)
    last_full = this_hour - timedelta(hours=1)
    assert 13.0 < rows[last_full]["mean"] < 13.8
    assert rows[last_full]["min"] < rows[last_full]["mean"] < rows[last_full]["max"]
    # BM2 records carry no SoC, so no SoC statistics are invented.
    assert "sensor.tractor_state_of_charge" not in stats

    # Downloading the same log again imports nothing.
    await coordinator._async_import(
        await coordinator.driver.async_read_history(bm2_device(history=volts), since=0)
    )
    assert coordinator.history_status["imported_hours"]["voltage"] == 0


async def test_gap_since_last_reading_sets_backfill_start(hass: HomeAssistant) -> None:
    entry = _entry()
    coordinator = await _setup_and_poll(hass, entry, BM2, bm2_device(history=[12.5] * 5))
    assert coordinator._history_since is None  # startup backfill done
    assert not coordinator._history_due()

    # Out of range for a while, then heard again: backfill from the last good reading.
    gap_start = time.time() - HISTORY_GAP - 60
    coordinator.last_success = gap_start
    fake = bm2_device(history=[12.5] * 5)
    seen: list[float] = []
    real = coordinator.driver.async_read_history

    async def spy(client, since):
        seen.append(since)
        return await real(client, since)

    coordinator.driver.async_read_history = spy
    fake.disconnect = AsyncMock()
    with (
        patch(
            "custom_components.ble_battery_monitor.coordinator.bluetooth.async_ble_device_from_address",
            return_value=BM2.device,
        ),
        patch(
            "custom_components.ble_battery_monitor.coordinator.bluetooth.async_last_service_info",
            return_value=BM2,
        ),
        patch(
            "custom_components.ble_battery_monitor.coordinator.establish_connection",
            AsyncMock(return_value=fake),
        ),
    ):
        coordinator.async_poll_now()
        # The base class debounces polls (10 s cooldown after the setup poll).
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert seen == [gap_start]
    assert coordinator._history_since is None


async def test_history_disabled_in_options(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"history": False})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not entry.runtime_data.history_enabled
    assert not entry.runtime_data._history_due()
    assert hass.states.get("button.tractor_fetch_history") is None
