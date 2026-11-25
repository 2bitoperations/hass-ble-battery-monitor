"""Simulated devices implementing just enough of BleakClient."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import struct

from custom_components.ble_battery_monitor.drivers import konnwei, leagend


@dataclass
class FakeChar:
    uuid: str
    properties: list[str]


@dataclass
class FakeService:
    uuid: str
    characteristics: list[FakeChar]


def uuid16(short: str) -> str:
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


@dataclass
class FakeClient:
    address: str
    services: list[FakeService]
    on_write: Callable[[FakeClient, FakeChar, bytes], None]
    _callbacks: dict[str, Callable] = field(default_factory=dict)
    writes: list[bytes] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)

    async def start_notify(self, char: FakeChar, cb: Callable) -> None:
        self._callbacks[char.uuid] = cb

    async def stop_notify(self, char: FakeChar) -> None:
        self._callbacks.pop(char.uuid, None)
        for t in self.tasks:
            t.cancel()

    async def write_gatt_char(self, char: FakeChar, data: bytes, response: bool = False) -> None:
        self.writes.append(bytes(data))
        self.on_write(self, char, bytes(data))

    def notify(self, short: str, data: bytes) -> None:
        cb = self._callbacks.get(uuid16(short))
        if cb:
            asyncio.get_running_loop().call_soon(cb, None, bytearray(data))


def konnwei_reply(cmd: int, payload: bytes) -> bytes:
    body = b"\x24\x24" + struct.pack("<H", len(payload) + 10) + struct.pack(">H", cmd) + payload
    return body + struct.pack("<H", konnwei.crc16_x25(body)) + b"\r\n"


@dataclass
class KonnweiLog:
    """BK400 data-logging state, as observed live."""

    schedules: list[tuple[int, int, int, int]] = field(default_factory=list)
    collecting: bool = False
    #: Samples logged by the running schedule so far (volts).
    pending: list[float] = field(default_factory=list)
    records: list[tuple[int, int, list[float]]] = field(default_factory=list)
    commands: list[int] = field(default_factory=list)
    corrupt: bool = False

    def handle(self, client: FakeClient, cmd: int, payload: bytes) -> bool:
        self.commands.append(cmd)
        send = lambda c, p=b"": client.notify("fff1", konnwei_reply(c, p))  # noqa: E731
        if cmd == 0x0B01:
            used = sum(d // i for _s, d, i, on in self.schedules if on)
            send(0x4B01, struct.pack("<I", 20480 - used))
        elif cmd == 0x0B09:
            send(0x4B09, bytes([1 if self.collecting else 0]))
        elif cmd == 0x0B03:
            send(0x4B03, b"".join(struct.pack("<IIIB", *e) for e in self.schedules))
        elif cmd == 0x0B02:
            self.schedules = [
                struct.unpack_from("<IIIB", payload, i) for i in range(0, len(payload), 13)
            ]
            self.collecting = any(e[3] for e in self.schedules)
            send(0x4B02, b"\x00")
        elif cmd == 0x0B0A:
            if self.collecting and self.pending:
                start, _d, interval, _on = self.schedules[0]
                self.records.append((start, interval, self.pending))
            self.pending, self.collecting = [], False
            send(0x4B0A)
        elif cmd == 0x0B04:
            self._download(client)
        elif cmd == 0x0B05:
            self.records.clear()
            send(0x4B05, b"\x00")
        else:
            return False
        return True

    def _download(self, client: FakeClient) -> None:
        body = struct.pack("<H", len(self.records)) + b"\xfe\xfe"
        for start, interval, volts in self.records:
            body += struct.pack("<III", start, interval, len(volts))
            body += b"".join(struct.pack("<H", round(v * 100)) for v in volts) + b"\xfe\xfe"
        head = b"\x24\x24" + struct.pack("<H", 0x1E) + b"\x4b\x04"  # LEN doesn't match (as live)
        raw = head + body
        crc = konnwei.crc16_x25(raw) ^ (1 if self.corrupt else 0)
        raw += struct.pack("<H", crc) + b"\r\n"
        sample = struct.pack("<H", 0x4000 | 1248)
        for i in range(0, len(raw), 17):
            client.notify("fff1", raw[i : i + 17])
            client.notify("fff1", sample)  # live stream keeps going in between
        client.notify("fff1", sample + sample)


def konnwei_device(
    *,
    volts: float = 12.65,
    temp: float = 21.37,
    streaming: bool = True,
    mode: int = 0,
    dev_name: bytes = b"BK400",
    answer: set[int] | None = None,
    log: KonnweiLog | None = None,
) -> FakeClient:
    """BK400 (streaming=True) or BK300-like (streaming=False, answers 0B0B).

    log: simulate on-device data logging (schedules, records, download, clear).
    """
    svc = FakeService(
        uuid16("fff0"),
        [
            FakeChar(uuid16("fff1"), ["notify"]),
            FakeChar(uuid16("fff2"), ["write-without-response", "write"]),
        ],
    )

    def on_write(client: FakeClient, _char: FakeChar, data: bytes) -> None:
        assert data[:2] == b"\x40\x40" and data[-2:] == b"\r\n"
        n = struct.unpack_from("<H", data, 2)[0]
        assert n == len(data)
        assert struct.unpack_from("<H", data, n - 4)[0] == konnwei.crc16_x25(data[: n - 4])
        cmd = struct.unpack_from(">H", data, 4)[0]
        if answer is not None and cmd not in answer:
            return
        if cmd == 0x0203:
            client.notify("fff1", konnwei_reply(0x4203, bytes([mode])))
        elif cmd == 0x0301:
            p = (
                dev_name.ljust(10, b"\0")
                + b"V1.0".ljust(10, b"\0")
                + b"V2.3".ljust(10, b"\0")
                + b"\0" * 10
                + struct.pack("<HH", 128, 0)
            )
            client.notify("fff1", konnwei_reply(0x4301, p))
        elif cmd == 0x0100 and streaming:

            async def stream() -> None:
                while True:
                    w = struct.pack("<H", 0x4000 | round(volts * 100))
                    client.notify("fff1", w * 3)
                    await asyncio.sleep(0.2)

            client.tasks.append(asyncio.get_running_loop().create_task(stream()))
        elif log is not None and log.handle(client, cmd, data[6 : n - 4]):
            pass
        elif cmd == 0x0B01:
            client.notify("fff1", konnwei_reply(0x4B01, b"\x00"))
        elif cmd == 0x0B0B and not streaming:
            client.notify(
                "fff1", konnwei_reply(0x4B0B, struct.pack("<H", round(volts * 100)) + b"\x02\x01")
            )
        elif cmd == 0x0B0C:
            client.notify("fff1", konnwei_reply(0x4B0C, struct.pack("<h", round(temp * 100))))

    return FakeClient("B4:00:00:00:12:34", [svc], on_write)


def _blocks(data: bytes) -> list[bytes]:
    data = data.ljust(-(-len(data) // 16) * 16, b"\0")
    return [data[i : i + 16] for i in range(0, len(data), 16)]


def bm6_device(
    *,
    volts: float = 12.81,
    temp: int = -5,
    soc: int = 87,
    state: int = 2,
    history: list[tuple[float, int, int]] | None = None,
) -> FakeClient:
    """history: (volts, soc, temp) records, newest first."""
    svc = FakeService(
        uuid16("fff0"),
        [FakeChar(uuid16("fff3"), ["write"]), FakeChar(uuid16("fff4"), ["notify"])],
    )

    def on_write(client: FakeClient, _char: FakeChar, data: bytes) -> None:
        req = leagend.decrypt(data)
        if req[:3] == bytes.fromhex("d15507"):
            msg = (
                bytes.fromhex("d15507")
                + bytes([1 if temp < 0 else 0, abs(temp), state, soc])
                + round(volts * 100).to_bytes(2, "big")
                + b"\0" * 7
            )
            client.notify("fff4", leagend.encrypt(msg))
        elif req[:3] == bytes.fromhex("d15505") and history is not None:
            pages = req[7]
            recs = history[: pages * 256]
            data = b"".join(
                ((round(v * 100) << 20) | (s << 12) | (t << 4)).to_bytes(4, "big")
                for v, s, t in recs
            )
            data += bytes(4 * 3)  # empty slots
            out = [bytes.fromhex("d1550501") + bytes(12), *_blocks(data)]
            out.append(b"\xff\xfe\xfe" + (len(data) + 9).to_bytes(3, "big") + bytes(10))
            # Several blocks per notification, like a large MTU.
            for i in range(0, len(out), 4):
                client.notify("fff4", b"".join(leagend.encrypt(b) for b in out[i : i + 4]))

    return FakeClient("50:54:7B:00:00:B6", [svc], on_write)


def bm2_device(
    *,
    volts: float = 12.68,
    soc: int = 76,
    interval: float = 0.2,
    history: list[float] | None = None,
) -> FakeClient:
    """BM2: streams F5 packets once notifications are on.

    history: logged voltages, oldest first (E7/E3 download); None = ignores writes.
    """
    key = leagend.BM2_KEY

    def on_write(client: FakeClient, _char: FakeChar, data: bytes) -> None:
        req = leagend.decrypt(data, key)
        if history is None:
            return
        recs = b"".join(
            ((round(v * 100) << 20) | (0x1 << 16) | (0x2BC << 4) | 0x1).to_bytes(4, "big")
            for v in history
        )
        if req[:2] == b"\xe7\x01":
            client.notify("fff4", leagend.encrypt(b"\xe7" + len(recs).to_bytes(3, "big"), key))
        elif req[:3] == b"\xe3\x00\x00":
            assert int.from_bytes(req[3:7], "big") == len(recs)
            out = [b"\xff\xff\xfe", *_blocks(recs)]
            out.append(b"\xff\xfe\xfe" + (len(recs) + 9).to_bytes(3, "big"))
            for block in out:
                client.notify("fff4", leagend.encrypt(block, key))

    svc = FakeService(
        uuid16("fff0"),
        [FakeChar(uuid16("fff3"), ["write"]), FakeChar(uuid16("fff4"), ["notify"])],
    )

    class BM2Client(FakeClient):
        async def start_notify(self, char: FakeChar, cb: Callable) -> None:
            await super().start_notify(char, cb)

            async def stream() -> None:
                while True:
                    raw = (round(volts * 100) << 4) | 0x1
                    msg = bytes([0xF5]) + raw.to_bytes(2, "big") + bytes([soc]) + b"\0" * 12
                    self.notify("fff4", leagend.encrypt(msg, leagend.BM2_KEY))
                    await asyncio.sleep(interval)

            self.tasks.append(asyncio.get_running_loop().create_task(stream()))

    return BM2Client("50:54:7B:00:00:B2", [svc], on_write)
