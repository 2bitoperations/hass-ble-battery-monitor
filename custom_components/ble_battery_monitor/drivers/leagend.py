"""Leagend-family (BM6) driver.

Protocol (public; see e.g. KrystianD/bm2-battery-monitor, rafciq/BM6):
  * write FFF3, notify FFF4
  * every message is 16 bytes, AES-128-CBC, zero IV, static key per model
  * real-time request d1 55 07 00.. ; reply (decrypted):
      [0:3] d1 55 07   [3] temp sign (1 = negative)   [4] temp C
      [5] state (0 ok, 1 low voltage, 2 charging)   [6] SoC %
      [7:9] voltage u16 BE /100   [9:11], [11:13] rapid accel/decel counters

BM2/BM7 use other keys and layouts; not implemented yet.
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

BM6_KEY = bytes([108, 101, 97, 103, 101, 110, 100, 255, 254, 48, 49, 48, 48, 48, 48, 57])
REQ_REALTIME = bytes.fromhex("d1550700000000000000000000000000")
REPLY_REALTIME = bytes.fromhex("d15507")
REPLY_TIMEOUT = 10.0

# iBeacon (Apple, company 76) UUID advertised by BM6 units, and other
# manufacturer-data prefixes seen on BM6 variants (from the BM6 integration's matchers).
BM6_IBEACON_PREFIX = bytes.fromhex("02153ba29cd9a42c894856badaf2606ef777")
BM6_MFR_PREFIXES: dict[int, bytes] = {
    76: BM6_IBEACON_PREFIX,
    10346: bytes([29, 149, 234, 19, 43, 237, 65, 254, 240, 204, 248, 156, 169, 74]),
    58876: bytes([253, 27, 217, 56, 90, 6, 129, 161, 54, 12, 22, 118, 190, 164]),
    31781: bytes([68, 112, 123, 182, 119, 231, 64, 187, 175, 169, 78, 96, 220, 100]),
}

STATES = {0: "ok", 1: "low_voltage", 2: "charging"}


def _cipher(key: bytes) -> Cipher:
    return Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16))


def encrypt(data: bytes, key: bytes = BM6_KEY) -> bytes:
    enc = _cipher(key).encryptor()
    return enc.update(data) + enc.finalize()


def decrypt(data: bytes, key: bytes = BM6_KEY) -> bytes:
    dec = _cipher(key).decryptor()
    return dec.update(data) + dec.finalize()


def parse_realtime(msg: bytes) -> Reading:
    """Parse a decrypted d15507 reply."""
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


class LeagendDriver(Driver):
    """Leagend BM6."""

    key = "leagend"
    manufacturer = "Leagend"
    sensors = frozenset({"voltage", "temperature", "soc", "state"})

    @classmethod
    def detect(cls, adv: Advertisement) -> str | None:
        for company, prefix in BM6_MFR_PREFIXES.items():
            data = adv.manufacturer_data.get(company)
            if data is not None and bytes(data).startswith(prefix):
                return "BM6"
        if (adv.name or "").upper().startswith("BM6"):
            return "BM6"
        return None

    async def async_read(self, client: BleakClient) -> Reading:
        notify_char = find_char(client, SERVICE, want="notify", prefer=NOTIFY)
        write_char = find_char(client, SERVICE, want="write", prefer=WRITE)
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def on_notify(_char: object, data: bytearray) -> None:
            raw = bytes(data)
            if len(raw) % 16:
                self._trace("rx?", raw)
                return
            msg = decrypt(raw)
            self._trace("rx", msg)
            if msg[:3] == REPLY_REALTIME and not fut.done():
                fut.set_result(msg)

        # Subscribe first, then ask, so the reply can't be missed.
        await client.start_notify(notify_char, on_notify)
        try:
            self._trace("tx", REQ_REALTIME)
            await client.write_gatt_char(write_char, encrypt(REQ_REALTIME), response=True)
            try:
                async with asyncio.timeout(REPLY_TIMEOUT):
                    msg = await fut
            except TimeoutError as err:
                raise DriverError("no real-time reply") from err
        finally:
            with suppress(BleakError, EOFError):
                await client.stop_notify(notify_char)
        return parse_realtime(msg)
