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
BK300 voltage poll 0B0B and, if history is enabled, the app's data-logging
commands. Never OTA (03xx), delete-all (0A03), rename (0C02) or factory
(01/09/19xx) commands.

History (BK400; verified live 2026-11-24): the device only logs inside
"collection schedules" (0B02). HA keeps one rolling schedule (24 h, a sample
every 5 min = 288 of the 20480 slots), renewed when under 12 h is left. To
read it: 0B0A stops collecting (turning it into a record), 0B04 returns all
records, 0B05 clears them, 0B02 starts a new schedule. A running schedule
reads back as no records, hence the stop.
  0B02/4B03 entry: u32 start, u32 duration ms, u32 interval ms, u8 enabled
  4B04: u16 n, FE FE, then per record u32 start, u32 interval ms, u32 count,
        count x u16 centivolts, FE FE. Spread over several notifications; the
        LEN field doesn't match, but the CRC covers the raw frame.
  4B01 payload: u32 free sample slots (minus slots reserved by schedules).
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

from .base import Advertisement, Driver, DriverError, HistorySample, Reading, find_char

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
CMD_LOG_WRITE = 0x0B02
CMD_LOG_SCHEDULES = 0x0B03
CMD_LOG_DOWNLOAD = 0x0B04
CMD_LOG_CLEAR = 0x0B05
CMD_LOG_STATUS = 0x0B09
CMD_LOG_STOP = 0x0B0A

LOG_INTERVAL_MS = 5 * 60_000
LOG_DURATION_MS = 24 * 3_600_000  # the app's maximum
LOG_RENEW = 12 * 3600  # renew the schedule when less than this is left
LOG_START_DELAY = 5  # schedules must start in the future
LOG_SEPARATOR = b"\xfe\xfe"
DOWNLOAD_TIMEOUT = 30.0

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


def schedule_entry(start: int, duration_ms: int, interval_ms: int, enabled: bool = True) -> bytes:
    return struct.pack("<IIIB", start, duration_ms, interval_ms, 1 if enabled else 0)


def parse_schedules(payload: bytes) -> list[tuple[int, int, int, bool]]:
    """4B03: (start, duration ms, interval ms, enabled); 13- or 17-byte entries."""
    if not payload:
        return []
    stride = 17 if len(payload) % 17 == 0 and len(payload) % 13 else 13
    out = []
    for i in range(0, len(payload) - stride + 1, stride):
        start, dur, intv, flag = struct.unpack_from("<IIIB", payload, i)
        out.append((start, dur, intv, flag == 1))
    return out


def _skip_sep(data: bytes, pos: int) -> int:
    return pos + 2 if data[pos : pos + 2] == LOG_SEPARATOR else pos


def parse_records(payload: bytes) -> list[HistorySample]:
    """4B04 payload -> samples (record start + i * interval)."""
    if len(payload) < 2:
        raise DriverError("short 4B04")
    (n,) = struct.unpack_from("<H", payload)
    pos = _skip_sep(payload, 2)
    out: list[HistorySample] = []
    for _ in range(n):
        if pos + 12 > len(payload):
            raise DriverError("truncated 4B04 record header")
        start, interval_ms, count = struct.unpack_from("<III", payload, pos)
        pos += 12
        if pos + 2 * count > len(payload) or interval_ms == 0:
            raise DriverError("truncated 4B04 record")
        for i, (w,) in enumerate(struct.iter_unpack("<H", payload[pos : pos + 2 * count])):
            v = (w & 0x3FFF) / 100.0
            if 0.0 < v <= MAX_VOLTS:
                out.append(HistorySample(ts=start + i * interval_ms / 1000, voltage=v))
        pos = _skip_sep(payload, pos + 2 * count)
    return out


def find_frame_end(buf: bytes | bytearray) -> int | None:
    """End offset of a complete frame in buf: CRLF where the CRC also checks out."""
    i = buf.find(b"\r\n", 8)
    while i >= 0:
        n = i + 2
        if struct.unpack_from("<H", buf, n - 4)[0] == crc16_x25(bytes(buf[: n - 4])):
            return n
        i = buf.find(b"\r\n", i + 1)
    return None


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
        #: Set by the coordinator from options: manage on-device logging.
        self.logging_enabled = False
        self.supports_history = model == "BK400"
        #: From the last poll: (collecting, end of the running schedule).
        self.log_state: dict[str, object] | None = None

    @property
    def info(self) -> dict[str, str | int] | None:
        return self._info

    def history_wanted(self) -> bool:
        """True if the on-device schedule needs (re)starting soon."""
        state = self.log_state
        if not (self.logging_enabled and self.supports_history) or state is None:
            return False
        end = state.get("ends")
        return not state.get("collecting") or end is None or end - time.time() < LOG_RENEW

    async def async_read(self, client: BleakClient) -> Reading:
        session = _Session(self, client)
        await session.start()
        try:
            # Same opening sequence as the vendor app (DeviceViewModel).
            mode = await session.send(CMD_MODE)
            if mode is not None and mode.payload[:1] not in (b"", b"\x00"):
                raise DriverError("device is in firmware-update mode; not touching it")
            if self._info is None:
                info = await session.send(CMD_INFO)
                if info is not None:
                    self._info = parse_info(info.payload)
                    name = str(self._info.get("dev_name") or "")
                    if name:
                        self.details.model = name
                    self.details.sw_version = str(self._info.get("mcu_ver") or "") or None
                    self.details.hw_version = str(self._info.get("iap_ver") or "") or None
            await session.send(CMD_HOME, reply=False)
            await session.time_sync()

            volts = session.volts
            if not volts:
                with suppress(TimeoutError):
                    async with asyncio.timeout(STREAM_WAIT):
                        await session.got_volts.wait()
            if volts:
                await asyncio.sleep(STREAM_COLLECT)
            else:
                poll = await session.send(CMD_VOLTAGE)
                if poll is not None and (v := parse_voltage_poll(poll.payload)) is not None:
                    volts.append(v)

            temp_frame = await session.send(CMD_TEMPERATURE)
            if self.logging_enabled and self.supports_history:
                await self._read_log_state(session)
        finally:
            await session.stop()

        if not volts:
            raise DriverError("no voltage received")
        recent = volts[-10:]
        reading = Reading(
            voltage=round(statistics.median(recent), 2),
            temperature=parse_temperature(temp_frame.payload) if temp_frame else None,
        )
        reading.extra = {"samples": len(volts), "other_samples": session.other_samples}
        return reading

    async def _read_log_state(self, session: _Session) -> None:
        status = await session.send(CMD_LOG_STATUS)
        sched = await session.send(CMD_LOG_SCHEDULES)
        if status is None or sched is None:
            return  # unknown: leave the previous state
        schedules = parse_schedules(sched.payload)
        now = time.time()
        ends = [s + d / 1000 for s, d, _i, on in schedules if on and s + d / 1000 > now]
        self.log_state = {
            "collecting": status.payload[:1] not in (b"", b"\x00"),
            "ends": max(ends) if ends else None,
            "schedules": schedules,
            "free_slots": session.free_slots,
        }

    async def async_read_history(self, client: BleakClient, since: float) -> list[HistorySample]:
        """Stop logging, download and clear the log, then start a fresh schedule."""
        if not self.logging_enabled:
            return []
        session = _Session(self, client)
        await session.start()
        samples: list[HistorySample] = []
        error: str | None = None
        raw: bytes | None = None
        try:
            await session.time_sync()
            status = await session.send(CMD_LOG_STATUS)
            collecting = status is not None and status.payload[:1] not in (b"", b"\x00")
            if collecting and await session.send(CMD_LOG_STOP) is None:
                raise DriverError("no reply to stop-logging")
            raw = await session.download()
            downloaded = False
            if raw is None:
                error = "no log download"
            else:
                try:
                    samples = parse_records(raw[6:-4])
                except DriverError as err:
                    error = str(err)  # keep the log on the device; retried later
                else:
                    downloaded = True
            # Always restart logging, even if the download failed (the log is kept then).
            try:
                if downloaded and await session.send(CMD_LOG_CLEAR) is None:
                    # The log stays on the device and the next download repeats these
                    # records; the import skips hours it already has.
                    error = "no reply to clear-log"
                start = int(time.time()) + LOG_START_DELAY
                entry = schedule_entry(start, LOG_DURATION_MS, LOG_INTERVAL_MS)
                ack = await session.send(CMD_LOG_WRITE, entry)
            except (BleakError, EOFError) as err:
                if not downloaded:
                    raise
                error, ack = error or f"link lost after download ({err})", None
            if ack is None or ack.payload[:1] not in (b"", b"\x00"):
                error = error or "schedule not accepted"
                # Unknown whether it's logging: say not, so the next try rewrites it.
                self.log_state = {"collecting": False, "ends": None, "schedules": []}
            else:
                self.log_state = {
                    "collecting": True,
                    "ends": start + LOG_DURATION_MS / 1000,
                    "schedules": [(start, LOG_DURATION_MS, LOG_INTERVAL_MS, True)],
                    "free_slots": session.free_slots,
                }
        finally:
            await session.stop()
        self.history_info = {
            "records_bytes": None if raw is None else len(raw),
            "samples": len(samples),
            "error": error,
            "at": round(time.time()),
        }
        if error and not downloaded:
            raise DriverError(error)
        # Downloaded: return the samples even if a later step failed (history_info says so).
        return samples


class _Session:
    """One notify subscription: request/reply, the voltage stream, and log downloads."""

    def __init__(self, driver: KonnweiDriver, client: BleakClient) -> None:
        self.driver, self.client = driver, client
        self.notify_char = find_char(client, SERVICE, want="notify", prefer=NOTIFY)
        try:
            self.write_char = find_char(client, SERVICE, want="write-without-response")
            self.write_response = False
        except DriverError:
            self.write_char = find_char(client, SERVICE, want="write")
            self.write_response = True
        self.decoder = StreamDecoder()
        self.waiters: dict[int, asyncio.Future[Frame]] = {}
        self.volts: list[float] = []
        self.got_volts = asyncio.Event()
        self.other_samples: dict[int, int] = {}
        self.free_slots: int | None = None
        self._capture: bytearray | None = None
        self._captured: asyncio.Future[bytes] | None = None

    async def start(self) -> None:
        await self.client.start_notify(self.notify_char, self._on_notify)

    async def stop(self) -> None:
        with suppress(BleakError, EOFError):
            await self.client.stop_notify(self.notify_char)

    def _on_notify(self, _char: object, data: bytearray) -> None:
        raw = bytes(data)
        self.driver._trace("rx", raw)
        if self._capture is not None:
            raw = self._capture_feed(raw)
        if raw:
            self._dispatch(raw)

    def _capture_feed(self, raw: bytes) -> bytes:
        """Collect a multi-notification 4B04; return whatever isn't part of it."""
        buf = self._capture
        assert buf is not None
        if not buf:
            i = raw.find(b"\x24\x24")
            if i < 0 or raw[i + 4 : i + 6] != struct.pack(">H", CMD_LOG_DOWNLOAD | 0x4000):
                return raw
            if i:
                self._dispatch(raw[:i])
            raw = raw[i:]
        elif len(raw) == 2 and find_frame_end(buf + raw) is None:
            return raw  # a live voltage sample between fragments
        buf += raw
        end = find_frame_end(buf)
        if end is None:
            return b""
        frame, rest = bytes(buf[:end]), bytes(buf[end:])
        self._capture = None
        if self._captured is not None and not self._captured.done():
            self._captured.set_result(frame)
        return rest

    def _dispatch(self, raw: bytes) -> None:
        for item in self.decoder.feed(raw):
            if isinstance(item, Frame):
                if not item.crc_ok:
                    _LOGGER.debug("%s: CRC mismatch %r", self.client.address, item)
                    continue
                if item.cmd == CMD_TIME | 0x4000 and len(item.payload) >= 4:
                    self.free_slots = struct.unpack_from("<I", item.payload)[0]
                fut = self.waiters.pop(item.cmd, None)
                if fut is not None and not fut.done():
                    fut.set_result(item)
                else:
                    _LOGGER.debug("%s: unsolicited %r", self.client.address, item)
            else:
                kind, v = item
                if kind == 1 and 0.0 < v <= MAX_VOLTS:
                    self.volts.append(v)
                    self.got_volts.set()
                else:
                    self.other_samples[kind] = self.other_samples.get(kind, 0) + 1

    async def _write(self, cmd: int, payload: bytes) -> None:
        frame = build_request(cmd, payload)
        self.driver._trace("tx", frame)
        await self.client.write_gatt_char(self.write_char, frame, response=self.write_response)

    async def send(self, cmd: int, payload: bytes = b"", *, reply: bool = True) -> Frame | None:
        fut: asyncio.Future[Frame] | None = None
        if reply:
            fut = asyncio.get_running_loop().create_future()
            self.waiters[cmd | 0x4000] = fut
        await self._write(cmd, payload)
        if fut is None:
            return None
        try:
            async with asyncio.timeout(REPLY_TIMEOUT):
                return await fut
        except TimeoutError:
            _LOGGER.debug("%s: no reply to %04X", self.client.address, cmd)
            return None
        finally:
            self.waiters.pop(cmd | 0x4000, None)

    async def time_sync(self) -> Frame | None:
        return await self.send(CMD_TIME, struct.pack("<I", int(time.time())))

    async def download(self) -> bytes | None:
        """0B04 -> the complete raw 4B04 frame (CRC-checked), or None."""
        self._capture = bytearray()
        self._captured = asyncio.get_running_loop().create_future()
        try:
            await self._write(CMD_LOG_DOWNLOAD, b"")
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                return await self._captured
        except TimeoutError:
            return None
        finally:
            self._capture = None
            self._captured = None
