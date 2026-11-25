"""Service: send allow-listed KONNWEI commands and return the raw replies (research)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import time
from typing import Any

from bleak.exc import BleakError
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .const import DOMAIN
from .coordinator import BatteryMonitorCoordinator
from .drivers import konnwei
from .drivers.base import find_char

SERVICE_KONNWEI_COMMAND = "konnwei_command"
ATTR_DEVICE_ID = "device_id"
ATTR_COMMANDS = "commands"
ATTR_LISTEN = "listen"

#: Only commands the vendor app sends in normal use: status/info, time sync,
#: LED, and the data-logging set (0B01-0B0C). Never OTA, delete, rename or
#: factory commands.
KONNWEI_ALLOWED = {0x0100, 0x0203, 0x0204, 0x0301} | set(range(0x0B01, 0x0B0D))

KONNWEI_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_COMMANDS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_LISTEN, default=2.0): vol.All(
            vol.Coerce(float), vol.Range(min=0.2, max=30)
        ),
    }
)


def parse_command(text: str) -> tuple[int, bytes]:
    """'0B02:aabbcc' -> (0x0B02, b'\\xaa\\xbb\\xcc')."""
    cmd, _, payload = text.replace(" ", "").partition(":")
    try:
        code = int(cmd, 16)
        data = bytes.fromhex(payload)
    except ValueError as err:
        raise ServiceValidationError(f"bad command {text!r}: use CMD or CMD:PAYLOADHEX") from err
    if code not in KONNWEI_ALLOWED:
        raise ServiceValidationError(f"command {code:04X} is not allowed")
    return code, data


def _coordinator(hass: HomeAssistant, device_id: str) -> BatteryMonitorCoordinator:
    device = dr.async_get(hass).async_get(device_id)
    if device is not None:
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN and hasattr(entry, "runtime_data"):
                return entry.runtime_data
    raise ServiceValidationError("not a loaded BLE Battery Monitor device")


async def _konnwei_command(call: ServiceCall) -> ServiceResponse:
    coordinator = _coordinator(call.hass, call.data[ATTR_DEVICE_ID])
    if coordinator.driver.key != "konnwei":
        raise ServiceValidationError("not a KONNWEI device")
    commands = [parse_command(c) for c in call.data[ATTR_COMMANDS]]
    listen: float = call.data[ATTR_LISTEN]
    rx: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    decoder = konnwei.StreamDecoder()
    t0 = time.monotonic()

    def on_notify(_char: object, data: bytearray) -> None:
        raw = bytes(data)
        rx.append({"t": round(time.monotonic() - t0, 3), "hex": raw.hex()})
        for item in decoder.feed(raw):
            if isinstance(item, konnwei.Frame):
                frames.append(
                    {"cmd": f"{item.cmd:04X}", "payload": item.payload.hex(), "crc_ok": item.crc_ok}
                )

    async with coordinator.lock:
        try:
            client = await coordinator.async_connect()
        except BleakError as err:
            raise ServiceValidationError(f"connect failed: {err}") from err
        try:
            notify_char = find_char(client, konnwei.SERVICE, want="notify", prefer=konnwei.NOTIFY)
            try:
                write_char = find_char(client, konnwei.SERVICE, want="write-without-response")
                response = False
            except konnwei.DriverError:
                write_char = find_char(client, konnwei.SERVICE, want="write")
                response = True
            await client.start_notify(notify_char, on_notify)
            tx = []
            for code, payload in commands:
                frame = konnwei.build_request(code, payload)
                tx.append({"t": round(time.monotonic() - t0, 3), "hex": frame.hex()})
                await client.write_gatt_char(write_char, frame, response=response)
                await asyncio.sleep(listen)
            with suppress(BleakError, EOFError):
                await client.stop_notify(notify_char)
        finally:
            await client.disconnect()
    return {"tx": tx, "rx": rx, "frames": frames}


def async_setup_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_KONNWEI_COMMAND):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_KONNWEI_COMMAND,
        _konnwei_command,
        schema=KONNWEI_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
