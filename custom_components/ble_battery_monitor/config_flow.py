"""Config flow: Bluetooth discovery, manual pick from discovered devices, options."""

from __future__ import annotations

from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_DRIVER,
    CONF_MODEL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .drivers import Advertisement, detect


def _detect(info: BluetoothServiceInfoBleak) -> tuple[str, str] | None:
    found = detect(
        Advertisement(
            address=info.address,
            name=info.name if info.name != info.address else None,
            manufacturer_data=dict(info.manufacturer_data),
            service_uuids=list(info.service_uuids),
        )
    )
    return (found[0].key, found[1]) if found else None


def _title(model: str, address: str) -> str:
    return f"{model} {address[-5:].replace(':', '')}"


class BatteryMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._found: dict[str, tuple[str, str]] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device found by Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if (found := _detect(discovery_info)) is None:
            return self.async_abort(reason="not_supported")
        self._discovery = discovery_info
        self._found[discovery_info.address] = found
        self.context["title_placeholders"] = {"name": _title(found[1], discovery_info.address)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device."""
        assert self._discovery is not None
        address = self._discovery.address
        driver, model = self._found[address]
        if user_input is not None:
            return self._create(address, driver, model)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": _title(model, address), "address": address},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick from supported devices currently being heard."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            driver, model = self._found[address]
            return self._create(address, driver, model)

        configured = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in configured or info.address in self._found:
                continue
            if (found := _detect(info)) is not None:
                self._found[info.address] = found
        if not self._found:
            return self.async_abort(reason="no_devices_found")
        options = [
            selector.SelectOptionDict(value=addr, label=f"{model} ({addr})")
            for addr, (_driver, model) in self._found.items()
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    @callback
    def _create(self, address: str, driver: str, model: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=_title(model, address),
            data={CONF_ADDRESS: address, CONF_DRIVER: driver, CONF_MODEL: model},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BatteryMonitorOptionsFlow()


class BatteryMonitorOptionsFlow(OptionsFlow):
    """Poll interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=current): vol.All(
                        selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=MIN_POLL_INTERVAL,
                                max=MAX_POLL_INTERVAL,
                                step=10,
                                unit_of_measurement="s",
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Coerce(int),
                    )
                }
            ),
        )
