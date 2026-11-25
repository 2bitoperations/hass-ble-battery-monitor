"""Button: download the device's own log now and backfill missing hours."""

from __future__ import annotations

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BatteryMonitorConfigEntry
from .coordinator import BatteryMonitorCoordinator

FETCH_HISTORY = ButtonEntityDescription(
    key="fetch_history", translation_key="fetch_history", entity_category=EntityCategory.CONFIG
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryMonitorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.history_enabled:
        async_add_entities([FetchHistoryButton(coordinator, FETCH_HISTORY)])


class FetchHistoryButton(
    PassiveBluetoothCoordinatorEntity[BatteryMonitorCoordinator], ButtonEntity
):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: BatteryMonitorCoordinator, description: ButtonEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, coordinator.address)}
        )

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        self.coordinator.async_request_history()
