"""Leagend-family driver: BM6 and BM2.

Common (public; KrystianD/bm2-battery-monitor, haxrob.net BM2 write-up, rafciq/BM6):
  * service FFF0, write FFF3, notify FFF4
  * 16-byte blocks, AES-128-CBC, zero IV restarted for every block, static key per model

BM6: request d1 55 07 00..; reply (decrypted)
  [0:3] d1 55 07   [3] temp sign (1 = negative)   [4] temp C
  [5] state (0 ok, 1 low voltage, 2 charging)   [6] SoC %
  [7:9] voltage u16 BE /100   [9:11], [11:13] rapid accel/decel counters

BM2: streams after subscribing, no request. Reading packet (decrypted):
  [0] F5   [1:3] u16 BE >> 4 = centivolts (12 bits)   low nibble of [2] = unknown status
  [3] charge level % (per haxrob; KrystianD doesn't decode it)
  Other packet types (E7 history count, FF FF FE / FF FE FE history markers) are ignored.

BM7 uses yet another key/layout; not implemented.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from typing import TYPE_CHECKING

from bleak.exc import BleakError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .base import Advertisement, Driver, DriverError, Reading, find_char

if TYPE_CHECKING:
    from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

SERVICE = "fff0"
WRITE = "fff3"
NOTIFY = "fff4"

# b"leagend\xff\xfe" + model-specific digits
BM6_KEY = b"leagend\xff\xfe0100009"
BM2_KEY = b"leagend\xff\xfe1882466"

REQ_REALTIME = bytes.fromhex("d1550700000000000000000000000000")
REPLY_REALTIME = bytes.fromhex("d15507")
REPLY_TIMEOUT = 10.0
#: BM2 streams; the interval isn't documented, so allow a generous wait.
BM2_STREAM_TIMEOUT = 20.0
BM2_READING = 0xF5

# iBeacon (Apple, company 76) UUIDs, and other manufacturer-data prefixes seen on
# BM6 variants (from the BM6 integration's matchers).
BM6_IBEACON_PREFIX = bytes.fromhex("02153ba29cd9a42c894856badaf2606ef777")
BM2_IBEACON_PREFIX = bytes.fromhex("0215655f83caae16a10a702e31f30d58dd82")
BM6_MFR_PREFIXES: dict[int, bytes] = {
    76: BM6_IBEACON_PREFIX,
    10346: bytes([29, 149, 234, 19, 43, 237, 65, 254, 240, 204, 248, 156, 169, 74]),
    58876: bytes([253, 27, 217, 56, 90, 6, 129, 161, 54, 12, 22, 118, 190, 164]),
    31781: bytes([68, 112, 123, 182, 119, 231, 64, 187, 175, 169, 78, 96, 220, 100]),
}
#: Names BM2 units advertise (haxrob). Only trusted together with service FFF0.
BM2_NAMES = {"BATTERY MONITOR", "LI BATTERY MONITOR", "ZX-1689"}

STATES = {0: "ok", 1: "low_voltage", 2: "charging"}

MODEL_KEYS = {"BM6": BM6_KEY, "BM2": BM2_KEY}
MODEL_SENSORS = {
    "BM6": frozenset({"voltage", "temperature", "soc", "state"}),
    "BM2": frozenset({"voltage", "soc"}),
}


def _block_cipher(key: bytes) -> Cipher:
    return Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16))


def encrypt(data: bytes, key: bytes = BM6_KEY) -> bytes:
    """Encrypt, zero-padding to 16 bytes, restarting the IV for each block."""
    data = data.ljust(-(-len(data) // 16) * 16, b"\x00")
    out = b""
    for i in range(0, len(data), 16):
        enc = _block_cipher(key).encryptor()
        out += enc.update(data[i : i + 16]) + enc.finalize()
    return out


def decrypt(data: bytes, key: bytes = BM6_KEY) -> bytes:
    """Decrypt whole 16-byte blocks, restarting the IV for each block."""
    out = b""
    for i in range(0, len(data) - len(data) % 16, 16):
        dec = _block_cipher(key).decryptor()
        out += dec.update(data[i : i + 16]) + dec.finalize()
    return out


def parse_realtime(msg: bytes) -> Reading:
    """Parse a decrypted BM6 d15507 reply."""
    if len(msg) < 9 or msg[:3] != REPLY_REALTIME:
        raise DriverError(f"not a real-time reply: {msg.hex()}")
    temp = -msg[4] if msg[3] == 1 else msg[4]
    voltage = int.from_bytes(msg[7:9], "big") / 100
    reading = Reading(
        voltage=round(voltage, 2),
        temperature=float(temp),
        soc=msg[6] if msg[6] <= 100 else None,
        state=STATES.get(msg[5], "unknown"),
    )
    if len(msg) >= 13:
        reading.extra = {
            "rapid_acceleration": int.from_bytes(msg[9:11], "big"),
            "rapid_deceleration": int.from_bytes(msg[11:13], "big"),
        }
    return reading


def parse_bm2(msg: bytes) -> Reading:
    """Parse a decrypted BM2 F5 packet."""
    if len(msg) < 4 or msg[0] != BM2_READING:
        raise DriverError(f"not a BM2 reading: {msg.hex()}")
    raw = int.from_bytes(msg[1:3], "big")
    return Reading(
        voltage=round((raw >> 4) / 100, 2),
        soc=msg[3] if msg[3] <= 100 else None,
        extra={"status_nibble": raw & 0x0F},
    )


class LeagendDriver(Driver):
    """Leagend BM6 / BM2."""

    key = "leagend"
    manufacturer = "Leagend"
    sensors = MODEL_SENSORS["BM6"]

    def __init__(self, model: str) -> None:
        super().__init__(model)
        if model not in MODEL_KEYS:
            raise ValueError(f"unsupported Leagend model {model}")
        self._key = MODEL_KEYS[model]
        self.sensors = MODEL_SENSORS[model]

    @classmethod
    def detect(cls, adv: Advertisement) -> str | None:
        apple = adv.manufacturer_data.get(76)
        if apple is not None and bytes(apple).startswith(BM2_IBEACON_PREFIX):
            return "BM2"
        for company, prefix in BM6_MFR_PREFIXES.items():
            data = adv.manufacturer_data.get(company)
            if data is not None and bytes(data).startswith(prefix):
                return "BM6"
        name = (adv.name or "").strip().upper()
        if name.startswith("BM6"):
            return "BM6"
        if name in BM2_NAMES and any(SERVICE in u.lower() for u in adv.service_uuids):
            return "BM2"
        return None

    async def async_read(self, client: BleakClient) -> Reading:
        if self.details.model == "BM2":
            return await self._read_bm2(client)
        return await self._read_bm6(client)

    async def _read_bm6(self, client: BleakClient) -> Reading:
        notify_char = find_char(client, SERVICE, want="notify", prefer=NOTIFY)
        write_char = find_char(client, SERVICE, want="write", prefer=WRITE)
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def on_notify(_char: object, data: bytearray) -> None:
            raw = bytes(data)
            if len(raw) % 16:
                self._trace("rx?", raw)
                return
            msg = decrypt(raw, self._key)
            self._trace("rx", msg)
            if msg[:3] == REPLY_REALTIME and not fut.done():
                fut.set_result(msg)

        # Subscribe first, then ask, so the reply can't be missed.
        await client.start_notify(notify_char, on_notify)
        try:
            self._trace("tx", REQ_REALTIME)
            await client.write_gatt_char(
                write_char, encrypt(REQ_REALTIME, self._key), response=True
            )
            try:
                async with asyncio.timeout(REPLY_TIMEOUT):
                    msg = await fut
            except TimeoutError as err:
                raise DriverError("no real-time reply") from err
        finally:
            with suppress(BleakError, EOFError):
                await client.stop_notify(notify_char)
        return parse_realtime(msg)

    async def _read_bm2(self, client: BleakClient) -> Reading:
        notify_char = find_char(client, SERVICE, want="notify", prefer=NOTIFY)
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def on_notify(_char: object, data: bytearray) -> None:
            raw = bytes(data)
            if len(raw) < 16:
                self._trace("rx?", raw)
                return
            msg = decrypt(raw, self._key)
            self._trace("rx", msg)
            if msg[0] == BM2_READING and not fut.done():
                fut.set_result(msg)

        await client.start_notify(notify_char, on_notify)
        try:
            try:
                async with asyncio.timeout(BM2_STREAM_TIMEOUT):
                    msg = await fut
            except TimeoutError as err:
                raise DriverError("no BM2 reading streamed") from err
        finally:
            with suppress(BleakError, EOFError):
                await client.stop_notify(notify_char)
        return parse_bm2(msg)
