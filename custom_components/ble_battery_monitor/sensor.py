"""Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BatteryMonitorConfigEntry
from .coordinator import BatteryMonitorCoordinator
from .drivers import Reading

STATE_OPTIONS = ["ok", "low_voltage", "charging", "unknown"]


@dataclass(frozen=True, kw_only=True)
class MonitorSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Reading], float | int | str | None]


READING_SENSORS: tuple[MonitorSensorDescription, ...] = (
    MonitorSensorDescription(
        key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda r: r.voltage,
    ),
    MonitorSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.temperature,
    ),
    MonitorSensorDescription(
        key="soc",
        translation_key="soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.soc,
    ),
    MonitorSensorDescription(
        key="state",
        translation_key="state",
        device_class=SensorDeviceClass.ENUM,
        options=STATE_OPTIONS,
        value_fn=lambda r: r.state,
    ),
)

RSSI_SENSOR = SensorEntityDescription(
    key="rssi",
    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryMonitorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    supported = coordinator.driver.sensors
    entities: list[SensorEntity] = [
        ReadingSensor(coordinator, d) for d in READING_SENSORS if d.key in supported
    ]
    entities.append(RssiSensor(coordinator, RSSI_SENSOR))
    async_add_entities(entities)


class _MonitorEntity(PassiveBluetoothCoordinatorEntity[BatteryMonitorCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: BatteryMonitorCoordinator, description: SensorEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address = coordinator.address
        details = coordinator.driver.details
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=coordinator.entry.title,
            manufacturer=details.manufacturer,
            model=details.model,
            sw_version=details.sw_version,
        )


class ReadingSensor(_MonitorEntity):
    """A value from the last successful poll."""

    entity_description: MonitorSensorDescription

    @property
    def available(self) -> bool:
        # Not tied to advert freshness: the BK400 may only be heard every few minutes.
        return self.coordinator.has_fresh_data and self.native_value is not None

    @property
    def native_value(self) -> float | int | str | None:
        data = self.coordinator.data
        return None if data is None else self.entity_description.value_fn(data)


class RssiSensor(_MonitorEntity):
    """Signal strength of the last advertisement (from whichever scanner heard it)."""

    @property
    def native_value(self) -> int | None:
        info = self.coordinator._last_service_info
        return info.rssi if info else None
