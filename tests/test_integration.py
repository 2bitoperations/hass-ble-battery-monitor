"""Config flow, setup and a full poll through Home Assistant."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ble_battery_monitor.const import CONF_DRIVER, CONF_MODEL, DOMAIN
from custom_components.ble_battery_monitor.drivers import konnwei, leagend

from .fakes import bm2_device, bm6_device, konnwei_device

FFF0 = "0000fff0-0000-1000-8000-00805f9b34fb"


def service_info(address: str, mfr: dict[int, bytes], uuids: list[str], name: str | None = None):
    device = BLEDevice(address, name, None)
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data=mfr,
        service_data={},
        service_uuids=uuids,
        tx_power=None,
        rssi=-70,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name or address,
        address=address,
        rssi=-70,
        manufacturer_data=mfr,
        service_data={},
        service_uuids=uuids,
        source="aa:bb:cc:dd:ee:ff",
        device=device,
        advertisement=adv,
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
        raw=None,
    )


BK400 = service_info("B4:00:00:00:12:34", {0x00B4: bytes.fromhex("00001234")}, [FFF0])
BM2 = service_info(
    "50:54:7B:00:00:B2",
    {76: bytes.fromhex("0215655f83caae16a10a702e31f30d58dd8200010002c5")},
    [FFF0],
    "Battery Monitor",
)
SHIELD = service_info("AA:BB:CC:DD:EE:FF", {}, [FFF0], "SHIELD Remote")


@pytest.fixture(autouse=True)
def _bt(mock_bluetooth, monkeypatch):
    monkeypatch.setattr(konnwei, "REPLY_TIMEOUT", 0.3)
    monkeypatch.setattr(konnwei, "STREAM_WAIT", 0.6)
    monkeypatch.setattr(konnwei, "STREAM_COLLECT", 0.3)
    monkeypatch.setattr(konnwei, "DOWNLOAD_TIMEOUT", 0.5)
    monkeypatch.setattr(leagend, "HISTORY_IDLE", 0.3)
    monkeypatch.setattr(leagend, "BM2_HISTORY_PAUSE", 0.0)


async def test_bluetooth_discovery_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=BK400
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "BK400 1234"
    assert result["data"] == {
        CONF_ADDRESS: BK400.address,
        CONF_DRIVER: "konnwei",
        CONF_MODEL: "BK400",
    }


async def test_unsupported_discovery_aborts(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=SHIELD
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_supported"


async def test_user_flow_lists_only_supported(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.ble_battery_monitor.config_flow.async_discovered_service_info",
        return_value=[BK400, SHIELD],
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        options = result["data_schema"].schema[CONF_ADDRESS].config["options"]
        assert [o["value"] for o in options] == [BK400.address]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: BK400.address}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_nothing_found(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.ble_battery_monitor.config_flow.async_discovered_service_info",
        return_value=[SHIELD],
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_devices_found"


async def _setup_and_poll(hass, entry, info, fake_client):
    entry.add_to_hass(hass)
    fake_client.disconnect = AsyncMock()
    fake_client.clear_cache = AsyncMock()
    with (
        patch(
            "custom_components.ble_battery_monitor.coordinator.bluetooth.async_ble_device_from_address",
            return_value=info.device,
        ),
        patch(
            "custom_components.ble_battery_monitor.coordinator.establish_connection",
            AsyncMock(return_value=fake_client),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator._async_handle_bluetooth_event(info, BluetoothChange.ADVERTISEMENT)
        await hass.async_block_till_done(wait_background_tasks=True)
    fake_client.disconnect.assert_awaited()
    return coordinator


async def test_bk400_poll_creates_sensors(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BK400.address,
        title="BK400 1234",
        data={CONF_ADDRESS: BK400.address, CONF_DRIVER: "konnwei", CONF_MODEL: "BK400"},
    )
    coordinator = await _setup_and_poll(hass, entry, BK400, konnwei_device(volts=12.65, temp=21.37))
    assert coordinator.last_poll_successful, coordinator.last_error
    assert hass.states.get("sensor.bk400_1234_voltage").state == "12.65"
    assert hass.states.get("sensor.bk400_1234_temperature").state == "21.4"
    from homeassistant.helpers import device_registry as dr

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert (device.model, device.sw_version, device.hw_version) == ("BK400", "V2.3", "V1.0")
    assert hass.states.get("sensor.bk400_1234_state_of_charge") is None  # BK400 doesn't report SoC
    # ...so it's estimated (default flooded, 12 V): 12.65 V at 21.4 C -> 93 %
    assert hass.states.get("sensor.bk400_1234_estimated_state_of_charge").state == "93"
    assert entity_registry.async_get("sensor.bk400_1234_signal_strength").disabled_by is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_bm6_poll(hass: HomeAssistant) -> None:
    info = service_info(
        "50:54:7B:00:00:B6",
        {76: bytes.fromhex("02153ba29cd9a42c894856badaf2606ef77711640000cd")},
        [],
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=info.address,
        title="BM6 00B6",
        data={CONF_ADDRESS: info.address, CONF_DRIVER: "leagend", CONF_MODEL: "BM6"},
    )
    await _setup_and_poll(hass, entry, info, bm6_device(volts=12.81, temp=-5, soc=87, state=2))
    assert hass.states.get("sensor.bm6_00b6_voltage").state == "12.81"
    assert hass.states.get("sensor.bm6_00b6_state_of_charge").state == "87"
    assert hass.states.get("sensor.bm6_00b6_battery_state").state == "charging"
    assert hass.states.get("sensor.bm6_00b6_estimated_state_of_charge") is None  # native SoC


async def test_failed_poll_leaves_entities_unavailable(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BK400.address,
        title="BK400 1234",
        data={CONF_ADDRESS: BK400.address, CONF_DRIVER: "konnwei", CONF_MODEL: "BK400"},
    )
    coordinator = await _setup_and_poll(hass, entry, BK400, konnwei_device(answer=set()))
    assert not coordinator.last_poll_successful
    assert "no voltage" in coordinator.last_error
    assert hass.states.get("sensor.bk400_1234_voltage").state == "unavailable"


async def test_bm2_discovery_with_name_then_poll(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=BM2
    )
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Tractor"})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["title"] == "Tractor"
    assert result["data"][CONF_MODEL] == "BM2"
    data = dict(result["data"])
    await hass.config_entries.async_remove(result["result"].entry_id)
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain=DOMAIN, unique_id=BM2.address, title="Tractor", data=data)
    await _setup_and_poll(hass, entry, BM2, bm2_device(volts=12.68, soc=76))
    assert hass.states.get("sensor.tractor_voltage").state == "12.68"
    assert hass.states.get("sensor.tractor_state_of_charge").state == "76"
    assert hass.states.get("sensor.tractor_temperature") is None


async def test_static_adverts_still_poll_via_timer(hass: HomeAssistant) -> None:
    """Regression: HA skips callbacks for unchanged adverts; the timer must keep polling."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.ble_battery_monitor.const import POLL_TIMER

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BK400.address,
        title="BK400 1234",
        data={CONF_ADDRESS: BK400.address, CONF_DRIVER: "konnwei", CONF_MODEL: "BK400"},
    )
    entry.add_to_hass(hass)
    connect = AsyncMock(side_effect=lambda *a, **k: _fake_bk400())
    latest = {"info": BK400}
    base = "custom_components.ble_battery_monitor.coordinator.bluetooth"
    with (
        patch(f"{base}.async_ble_device_from_address", return_value=BK400.device),
        patch(f"{base}.async_last_service_info", side_effect=lambda *a, **k: latest["info"]),
        patch("custom_components.ble_battery_monitor.coordinator.establish_connection", connect),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator._async_handle_bluetooth_event(BK400, BluetoothChange.ADVERTISEMENT)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert connect.await_count == 1

        # Device not heard since the last poll: timer must not poll.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=POLL_TIMER + 1))
        await hass.async_block_till_done(wait_background_tasks=True)
        assert connect.await_count == 1

        # Same advert content heard again an interval later: timer polls.
        latest["info"] = service_info(
            BK400.address, dict(BK400.manufacturer_data), list(BK400.service_uuids)
        )
        object.__setattr__(latest["info"], "time", time.monotonic() + coordinator.poll_interval + 5)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2 * POLL_TIMER + 2))
        await hass.async_block_till_done(wait_background_tasks=True)
        assert connect.await_count == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


def _fake_bk400():
    client = konnwei_device()
    client.disconnect = AsyncMock()
    client.clear_cache = AsyncMock()
    return client


async def test_options_flow_over_http(hass: HomeAssistant, hass_client) -> None:
    """Render and submit options through the HTTP API (catches schema-serialization 500s)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BK400.address,
        title="BK400 1234",
        data={CONF_ADDRESS: BK400.address, CONF_DRIVER: "konnwei", CONF_MODEL: "BK400"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    assert resp.status == 200, await resp.text()
    form = await resp.json()
    assert {f["name"] for f in form["data_schema"]} == {
        "poll_interval",
        "history",
        "chemistry",
        "nominal_voltage",
    }
    resp = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}",
        json={"poll_interval": 300, "history": False, "chemistry": "agm", "nominal_voltage": "12"},
    )
    assert resp.status == 200, await resp.text()
    await hass.async_block_till_done()
    assert entry.options == {
        "poll_interval": 300,
        "history": False,
        "chemistry": "agm",
        "nominal_voltage": "12",
    }
    assert not entry.runtime_data.driver.logging_enabled
    assert entry.runtime_data.chemistry == "agm"
    assert entry.runtime_data.poll_interval == 300


async def test_options_flow_native_soc_has_no_battery_type(
    hass: HomeAssistant, hass_client
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="50:54:7B:00:00:B2",
        title="Tractor",
        data={CONF_ADDRESS: "50:54:7B:00:00:B2", CONF_DRIVER: "leagend", CONF_MODEL: "BM2"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    assert resp.status == 200, await resp.text()
    # Native SoC: no battery type. BM2 logs history: the backfill switch.
    assert [f["name"] for f in (await resp.json())["data_schema"]] == ["poll_interval", "history"]


async def test_konnwei_command_service(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import ServiceValidationError
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BK400.address,
        title="BK400 1234",
        data={CONF_ADDRESS: BK400.address, CONF_DRIVER: "konnwei", CONF_MODEL: "BK400"},
    )
    await _setup_and_poll(hass, entry, BK400, konnwei_device())
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    fake = konnwei_device(temp=19.5)
    fake.disconnect = AsyncMock()
    base = "custom_components.ble_battery_monitor.coordinator"
    with (
        patch(f"{base}.bluetooth.async_ble_device_from_address", return_value=BK400.device),
        patch(f"{base}.establish_connection", AsyncMock(return_value=fake)),
    ):
        resp = await hass.services.async_call(
            DOMAIN,
            "konnwei_command",
            {"device_id": device.id, "commands": ["0B0C"], "listen": 0.3},
            blocking=True,
            return_response=True,
        )
        assert resp["tx"][0]["hex"] == "40400a000b0c16c60d0a"
        assert resp["frames"] == [{"cmd": "4B0C", "payload": "9e07", "crc_ok": True}]
        fake.disconnect.assert_awaited()
        for bad in ("0A03", "0302", "0C02:00", "zz"):
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN,
                    "konnwei_command",
                    {"device_id": device.id, "commands": [bad]},
                    blocking=True,
                    return_response=True,
                )


def _bk400_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=BK400.address,
        title="BK400 1234",
        data={CONF_ADDRESS: BK400.address, CONF_DRIVER: "konnwei", CONF_MODEL: "BK400"},
    )


def _heard_at(t: float):
    info = service_info(BK400.address, dict(BK400.manufacturer_data), list(BK400.service_uuids))
    object.__setattr__(info, "time", t)
    return info


async def test_failed_poll_backs_off_and_waits_for_a_fresh_advert(hass: HomeAssistant) -> None:
    """Marginal link: don't reconnect blind every minute; wait for a new sighting, back off."""
    coordinator = await _setup_and_poll(hass, _bk400_entry(), BK400, konnwei_device(answer=set()))
    assert not coordinator.last_poll_successful
    assert coordinator.consecutive_failures == 1
    attempt = coordinator._last_attempt
    assert attempt is not None

    base = "custom_components.ble_battery_monitor.coordinator.bluetooth"
    with patch(f"{base}.async_ble_device_from_address", return_value=BK400.device):
        # The timer re-feeds the sighting the failed attempt already used: no retry.
        assert not coordinator._needs_poll(_heard_at(attempt - 1), 3600)
        # Heard again since then: retry once the back-off has passed.
        fresh = _heard_at(attempt + 5)
        assert not coordinator._needs_poll(fresh, 30)
        assert coordinator._needs_poll(fresh, 61)

    # 60 s, then doubling, capped at 10 min (poll interval 120 s).
    dues = []
    for n in range(1, 7):
        coordinator.consecutive_failures = n
        dues.append(coordinator.retry_due)
    assert dues == [60, 120, 240, 480, 600, 600]
    coordinator.consecutive_failures = 0
    assert coordinator.retry_due == coordinator.poll_interval


async def test_success_resets_backoff(hass: HomeAssistant) -> None:
    coordinator = await _setup_and_poll(hass, _bk400_entry(), BK400, konnwei_device())
    assert coordinator.last_poll_successful and coordinator.consecutive_failures == 0
    base = "custom_components.ble_battery_monitor.coordinator.bluetooth"
    with patch(f"{base}.async_ble_device_from_address", return_value=BK400.device):
        # No failure outstanding: an old sighting is fine once the interval has passed.
        assert coordinator._needs_poll(_heard_at(0), coordinator.poll_interval + 1)


async def test_each_connect_attempt_asks_ha_for_the_best_proxy(hass: HomeAssistant) -> None:
    """Retries must not stick to the proxy that just failed."""
    entry = _bk400_entry()
    entry.add_to_hass(hass)
    first = BLEDevice(BK400.address, None, {"source": "gate"})
    better = BLEDevice(BK400.address, None, {"source": "other-proxy"})
    picks = iter([first, first, better])
    connect = AsyncMock(side_effect=lambda *a, **k: _fake_bk400())
    base = "custom_components.ble_battery_monitor.coordinator"
    with (
        patch(
            f"{base}.bluetooth.async_ble_device_from_address",
            side_effect=lambda *a, **k: next(picks, better),
        ),
        patch(f"{base}.establish_connection", connect),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator._async_handle_bluetooth_event(BK400, BluetoothChange.ADVERTISEMENT)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert connect.await_count == 1
        args, kwargs = connect.call_args
        assert args[1] is first
        # bleak-retry-connector calls this before each retry.
        assert kwargs["ble_device_callback"]() is better
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_poll_budget_fits_every_connect_attempt():
    """Regression: a 45 s budget cut the 3rd 20 s connect attempt off after ~5 s."""
    from bleak_retry_connector import BLEAK_TIMEOUT

    from custom_components.ble_battery_monitor.const import (
        CONNECT_ATTEMPT_TIMEOUT,
        CONNECT_ATTEMPTS,
        POLL_TIMEOUT,
    )

    assert CONNECT_ATTEMPT_TIMEOUT >= BLEAK_TIMEOUT
    assert POLL_TIMEOUT >= CONNECT_ATTEMPTS * CONNECT_ATTEMPT_TIMEOUT + 20  # + time for the read
