"""KONNWEI BK-series (BK400, BK300) driver.

Protocol notes: see PROTOCOL.md in the parent project (derived from the BK400
Android app com.jiawei.bk400 1.1.06) and picostar's BK300 notes.

Frames, all multi-byte fields little-endian except the command word:
  app -> dev  40 40 | len u16 | cmd u16 BE | payload | crc16/x25 u16 | 0D 0A
  dev -> app  24 24 | len u16 | cmd|0x4000 | payload | crc16/x25 u16 | 0D 0A
  len counts the whole frame. CRC covers everything before it.
Between frames the BK400 streams raw u16 samples: bits 15-14 type, 13-0 centivolts;
type 1 is the live battery voltage.

Only commands the vendor app sends on every connect are used here, plus the
BK300 voltage poll 0B0B. Never OTA (03xx), delete (0A03), rename (0C02),
logging (0B02-0B06/0B0A) or factory (01/09/19xx) commands.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import statistics
import struct
import time
from typing import TYPE_CHECKING

from bleak.exc import BleakError

from .base import Advertisement, Driver, DriverError, Reading, find_char

if TYPE_CHECKING:
    from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

SERVICE = "fff0"
NOTIFY = "fff1"

CMD_MODE = 0x0203
CMD_INFO = 0x0301
CMD_HOME = 0x0100
CMD_TIME = 0x0B01
CMD_VOLTAGE = 0x0B0B  # BK300 poll; BK400 normally streams instead
CMD_TEMPERATURE = 0x0B0C

REPLY_TIMEOUT = 3.0
STREAM_WAIT = 4.0  # how long to wait for the BK400 voltage stream
STREAM_COLLECT = 1.0  # after the first sample, keep listening this long
MAX_VOLTS = 60.0


def crc16_x25(data: bytes) -> int:
    """CRC-16/X-25 (PPP FCS-16)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def build_request(cmd: int, payload: bytes = b"") -> bytes:
    """Build an app->device frame."""
    body = b"\x40\x40" + struct.pack("<H", len(payload) + 10) + struct.pack(">H", cmd) + payload
    return body + struct.pack("<H", crc16_x25(body)) + b"\r\n"


class Frame:
    """A device->app frame."""

    __slots__ = ("cmd", "crc_ok", "payload")

    def __init__(self, cmd: int, payload: bytes, crc_ok: bool) -> None:
        self.cmd, self.payload, self.crc_ok = cmd, payload, crc_ok

    def __repr__(self) -> str:
        return f"Frame({self.cmd:04X}, {self.payload.hex()}, crc_ok={self.crc_ok})"


Sample = tuple[int, float]  # (type, volts)


class StreamDecoder:
    """Split notifications into frames and raw voltage samples."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._odd: int | None = None

    def feed(self, data: bytes) -> list[Frame | Sample]:
        out: list[Frame | Sample] = []
        buf = self._buf
        buf += data
        while buf:
            if buf[:2] == b"\x24\x24":
                if len(buf) < 4:
                    break
                n = struct.unpack_from("<H", buf, 2)[0]
                if 10 <= n <= 512 and len(buf) < n:
                    break  # rest arrives in a later notification
                if 10 <= n <= 512 and buf[n - 2 : n] == b"\r\n":
                    raw = bytes(buf[:n])
                    del buf[:n]
                    crc_ok = struct.unpack_from("<H", raw, n - 4)[0] == crc16_x25(raw[: n - 4])
                    out.append(Frame(struct.unpack_from(">H", raw, 4)[0], raw[6 : n - 4], crc_ok))
                    continue
                # "24 24" that isn't a frame: treat as a sample word
                out += self._samples(bytes(buf[:2]))
                del buf[:2]
                continue
            # Raw samples up to the next possible header (or everything).
            # Sample words can contain 24 24 by chance; the frame check above
            # falls back to treating them as samples.
            nxt = buf.find(b"\x24\x24")
            chunk = bytes(buf if nxt < 0 else buf[:nxt])
            del buf[: len(chunk)]
            out += self._samples(chunk)
        return out

    def _samples(self, chunk: bytes) -> list[Sample]:
        if self._odd is not None:
            chunk = bytes([self._odd]) + chunk
            self._odd = None
        if len(chunk) % 2:
            self._odd, chunk = chunk[-1], chunk[:-1]
        return [(w >> 14, (w & 0x3FFF) / 100.0) for (w,) in struct.iter_unpack("<H", chunk)]


def _ascii(b: bytes) -> str:
    return b.split(b"\x00")[0].decode("ascii", "replace").strip()


def parse_info(payload: bytes) -> dict[str, str | int]:
    """4301: devName, iapVer, mcuVer, flashVer (10 bytes ASCII each), mcuSize, flashSize."""
    info: dict[str, str | int] = {
        "dev_name": _ascii(payload[0:10]),
        "iap_ver": _ascii(payload[10:20]),
        "mcu_ver": _ascii(payload[20:30]),
        "flash_ver": _ascii(payload[30:40]),
    }
    if len(payload) >= 44:
        info["mcu_size"], info["flash_size"] = struct.unpack_from("<HH", payload, 40)
    return info


def parse_temperature(payload: bytes) -> float | None:
    """4B0C: s16 LE hundredths of a degree C."""
    if len(payload) < 2:
        return None
    t = round(struct.unpack_from("<h", payload)[0] / 100.0, 1)
    return t if -40.0 <= t <= 125.0 else None


def parse_voltage_poll(payload: bytes) -> float | None:
    """4B0B (BK300): u16 LE centivolts."""
    if len(payload) < 2:
        return None
    v = (struct.unpack_from("<H", payload)[0] & 0x3FFF) / 100.0
    return v if 0.0 < v <= MAX_VOLTS else None


def mac_bytes(address: str) -> bytes | None:
    try:
        return bytes.fromhex(address.replace(":", "").replace("-", ""))
    except ValueError:
        return None


class KonnweiDriver(Driver):
    """KONNWEI BK400 / BK300."""

    key = "konnwei"
    manufacturer = "KONNWEI"
    sensors = frozenset({"voltage", "temperature"})

    @classmethod
    def detect(cls, adv: Advertisement) -> str | None:
        """KONNWEI adverts carry FFF0 plus manufacturer data that spells the MAC.

        e.g. B4:00:00:00:12:34 -> company 0x00B4, data 00 00 12 34.
        """
        if not any(SERVICE in u.lower() for u in adv.service_uuids):
            return None
        mac = mac_bytes(adv.address)
        if mac is None:
            return None  # e.g. macOS UUID addresses
        for company, data in adv.manufacturer_data.items():
            if struct.pack("<H", company) + bytes(data) == mac:
                name = (adv.name or "").upper()
                return "BK300" if "BK300" in name else "BK400"
        return None

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self._info: dict[str, str | int] | None = None

    @property
    def info(self) -> dict[str, str | int] | None:
        return self._info

    async def async_read(self, client: BleakClient) -> Reading:
        notify_char = find_char(client, SERVICE, want="notify", prefer=NOTIFY)
        try:
            write_char = find_char(client, SERVICE, want="write-without-response")
            write_response = False
        except DriverError:
            write_char = find_char(client, SERVICE, want="write")
            write_response = True

        loop = asyncio.get_running_loop()
        decoder = StreamDecoder()
        waiters: dict[int, asyncio.Future[Frame]] = {}
        volts: list[float] = []
        got_volts = asyncio.Event()
        other_samples: dict[int, int] = {}

        def on_notify(_char: object, data: bytearray) -> None:
            raw = bytes(data)
            self._trace("rx", raw)
            for item in decoder.feed(raw):
                if isinstance(item, Frame):
                    if not item.crc_ok:
                        _LOGGER.debug("%s: CRC mismatch %r", client.address, item)
                        continue
                    fut = waiters.pop(item.cmd, None)
                    if fut is not None and not fut.done():
                        fut.set_result(item)
                    else:
                        _LOGGER.debug("%s: unsolicited %r", client.address, item)
                else:
                    kind, v = item
                    if kind == 1 and 0.0 < v <= MAX_VOLTS:
                        volts.append(v)
                        got_volts.set()
                    else:
                        other_samples[kind] = other_samples.get(kind, 0) + 1

        async def send(cmd: int, payload: bytes = b"", *, reply: bool = True) -> Frame | None:
            fut: asyncio.Future[Frame] | None = None
            if reply:
                fut = loop.create_future()
                waiters[cmd | 0x4000] = fut
            frame = build_request(cmd, payload)
            self._trace("tx", frame)
            await client.write_gatt_char(write_char, frame, response=write_response)
            if fut is None:
                return None
            try:
                async with asyncio.timeout(REPLY_TIMEOUT):
                    return await fut
            except TimeoutError:
                _LOGGER.debug("%s: no reply to %04X", client.address, cmd)
                return None
            finally:
                waiters.pop(cmd | 0x4000, None)

        await client.start_notify(notify_char, on_notify)
        try:
            # Same opening sequence as the vendor app (DeviceViewModel).
            mode = await send(CMD_MODE)
            if mode is not None and mode.payload[:1] not in (b"", b"\x00"):
                raise DriverError("device is in firmware-update mode; not touching it")
            if self._info is None:
                info = await send(CMD_INFO)
                if info is not None:
                    self._info = parse_info(info.payload)
                    name = str(self._info.get("dev_name") or "")
                    if name:
                        self.details.model = name
                    self.details.sw_version = str(self._info.get("mcu_ver") or "") or None
                    self.details.hw_version = str(self._info.get("iap_ver") or "") or None
            await send(CMD_HOME, reply=False)
            await send(CMD_TIME, struct.pack("<I", int(time.time())))

            if not volts:
                with suppress(TimeoutError):
                    async with asyncio.timeout(STREAM_WAIT):
                        await got_volts.wait()
            if volts:
                await asyncio.sleep(STREAM_COLLECT)
            else:
                poll = await send(CMD_VOLTAGE)
                if poll is not None and (v := parse_voltage_poll(poll.payload)) is not None:
                    volts.append(v)

            temp_frame = await send(CMD_TEMPERATURE)
        finally:
            with suppress(BleakError, EOFError):
                await client.stop_notify(notify_char)

        if not volts:
            raise DriverError("no voltage received")
        recent = volts[-10:]
        reading = Reading(
            voltage=round(statistics.median(recent), 2),
            temperature=parse_temperature(temp_frame.payload) if temp_frame else None,
        )
        reading.extra = {"samples": len(volts), "other_samples": other_samples}
        return reading
