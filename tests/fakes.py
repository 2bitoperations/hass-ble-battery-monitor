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


def konnwei_device(
    *,
    volts: float = 12.65,
    temp: float = 21.37,
    streaming: bool = True,
    mode: int = 0,
    dev_name: bytes = b"BK400",
    answer: set[int] | None = None,
) -> FakeClient:
    """BK400 (streaming=True) or BK300-like (streaming=False, answers 0B0B)."""
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
        elif cmd == 0x0B01:
            client.notify("fff1", konnwei_reply(0x4B01, b"\x00"))
        elif cmd == 0x0B0B and not streaming:
            client.notify(
                "fff1", konnwei_reply(0x4B0B, struct.pack("<H", round(volts * 100)) + b"\x02\x01")
            )
        elif cmd == 0x0B0C:
            client.notify("fff1", konnwei_reply(0x4B0C, struct.pack("<h", round(temp * 100))))

    return FakeClient("B4:00:00:00:12:34", [svc], on_write)


def bm6_device(
    *, volts: float = 12.81, temp: int = -5, soc: int = 87, state: int = 2
) -> FakeClient:
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

    return FakeClient("50:54:7B:00:00:B6", [svc], on_write)
