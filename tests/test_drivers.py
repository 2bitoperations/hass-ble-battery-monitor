"""Protocol and driver tests against simulated devices (no Home Assistant needed)."""

from __future__ import annotations

import struct

import pytest

from custom_components.ble_battery_monitor.drivers import (
    Advertisement,
    DriverError,
    detect,
    konnwei,
    leagend,
)
from custom_components.ble_battery_monitor.drivers.konnwei import (
    Frame,
    KonnweiDriver,
    StreamDecoder,
)
from custom_components.ble_battery_monitor.drivers.leagend import LeagendDriver

from .fakes import bm2_device, bm6_device, konnwei_device, konnwei_reply

BK400_ADV = Advertisement(
    address="B4:00:00:00:12:34",
    name=None,
    manufacturer_data={0x00B4: bytes.fromhex("00001234")},
    service_uuids=["0000fff0-0000-1000-8000-00805f9b34fb"],
)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(konnwei, "REPLY_TIMEOUT", 0.3)
    monkeypatch.setattr(konnwei, "STREAM_WAIT", 0.6)
    monkeypatch.setattr(konnwei, "STREAM_COLLECT", 0.3)


def test_app_hardcoded_frame():
    # The one literal frame in the BK400 app (HexUtil#sendWriteData, "0301").
    assert konnwei.build_request(0x0301).hex().upper() == "40400A00030133D30D0A"


def test_decoder_frames_and_samples_split_across_notifications():
    d = StreamDecoder()
    t = konnwei_reply(0x4B0C, struct.pack("<h", 2345))
    items = d.feed(struct.pack("<HH", 0x4000 | 1265, 0) + t[:5]) + d.feed(t[5:])
    assert items[:2] == [(1, 12.65), (0, 0.0)]
    assert isinstance(items[2], Frame) and items[2].crc_ok and items[2].cmd == 0x4B0C
    w = struct.pack("<H", 0x4000 | 1301)
    assert d.feed(w[:1]) == [] and d.feed(w[1:]) == [(1, 13.01)]


def test_decoder_fake_header_in_samples():
    # A sample word that happens to be 24 24 must not swallow the stream.
    d = StreamDecoder()
    items = d.feed(b"\x24\x24" + struct.pack("<H", 0x4000 | 1250) * 3)
    assert (1, 12.5) in items


def test_detect():
    assert detect(BK400_ADV) == (KonnweiDriver, "BK400")
    wrong_mac = Advertisement(
        "B4:00:00:00:03:7A", None, BK400_ADV.manufacturer_data, BK400_ADV.service_uuids
    )
    assert detect(wrong_mac) is None
    no_fff0 = Advertisement(BK400_ADV.address, None, BK400_ADV.manufacturer_data, [])
    assert detect(no_fff0) is None
    bm6 = Advertisement(
        "50:54:7B:00:00:B6", None, {76: leagend.BM6_IBEACON_PREFIX + b"\x11\x64\x00\x00\xcd"}, []
    )
    assert detect(bm6) == (LeagendDriver, "BM6")
    bm2 = Advertisement(
        "50:54:7B:00:00:B2",
        "Battery Monitor",
        {76: bytes.fromhex("0215655f83caae16a10a702e31f30d58dd8200010002c5")},
        ["0000fff0-0000-1000-8000-00805f9b34fb"],
    )
    assert detect(bm2) == (LeagendDriver, "BM2")
    # Generic FFF0 device with an unrelated name must not be claimed.
    shield = Advertisement(
        "AA:BB:CC:DD:EE:FF", "SHIELD Remote", {}, ["0000fff0-0000-1000-8000-00805f9b34fb"]
    )
    assert detect(shield) is None
    # BM2 by name needs FFF0 too.
    assert detect(Advertisement("AA:BB:CC:DD:EE:FF", "Battery Monitor", {}, [])) is None


async def test_bk400_streaming_read():
    drv = KonnweiDriver("BK400")
    client = konnwei_device(volts=12.65, temp=21.37)
    r = await drv.async_read(client)
    assert r.voltage == 12.65 and r.temperature == 21.4
    assert drv.details.model == "BK400" and drv.details.sw_version == "V2.3"
    sent = [struct.unpack_from(">H", w, 4)[0] for w in client.writes]
    assert sent == [0x0203, 0x0301, 0x0100, 0x0B01, 0x0B0C]
    # Second poll does not re-read device info.
    client2 = konnwei_device()
    await drv.async_read(client2)
    assert 0x0301 not in [struct.unpack_from(">H", w, 4)[0] for w in client2.writes]


async def test_bk300_poll_fallback():
    drv = KonnweiDriver("BK400")
    r = await drv.async_read(konnwei_device(volts=13.2, streaming=False, dev_name=b"BK300"))
    assert r.voltage == 13.2
    assert drv.details.model == "BK300"


async def test_silent_device_fails_fast():
    drv = KonnweiDriver("BK400")
    with pytest.raises(DriverError, match="no voltage"):
        await drv.async_read(konnwei_device(answer=set()))


async def test_bootloader_mode_refused():
    drv = KonnweiDriver("BK400")
    client = konnwei_device(mode=1)
    with pytest.raises(DriverError, match="firmware-update"):
        await drv.async_read(client)
    assert len(client.writes) == 1  # nothing after the mode query


async def test_bm6_read():
    r = await LeagendDriver("BM6").async_read(bm6_device(volts=12.81, temp=-5, soc=87, state=2))
    assert (r.voltage, r.temperature, r.soc, r.state) == (12.81, -5.0, 87, "charging")


async def test_bm6_timeout(monkeypatch):
    monkeypatch.setattr(leagend, "REPLY_TIMEOUT", 0.2)
    client = bm6_device()
    client.on_write = lambda *a: None
    with pytest.raises(DriverError, match="no real-time reply"):
        await LeagendDriver("BM6").async_read(client)


def test_leagend_keys_and_published_samples():
    assert (
        bytes([108, 101, 97, 103, 101, 110, 100, 255, 254, 48, 49, 48, 48, 48, 48, 57])
        == leagend.BM6_KEY
    )
    # haxrob: f54f414c... -> 12.68 V, 76 %;  KrystianD: F5508164... -> 12.88 V
    r = leagend.parse_bm2(bytes.fromhex("f54f414c0b0f") + b"\0" * 10)
    assert (r.voltage, r.soc) == (12.68, 76)
    assert leagend.parse_bm2(bytes.fromhex("f550816400fa") + b"\0" * 10).voltage == 12.88
    # per-block IV restart: two identical blocks encrypt identically
    blk = bytes(range(16))
    enc = leagend.encrypt(blk * 2, leagend.BM2_KEY)
    assert enc[:16] == enc[16:] and leagend.decrypt(enc, leagend.BM2_KEY) == blk * 2


async def test_bm2_streaming_read():
    drv = LeagendDriver("BM2")
    client = bm2_device(volts=12.68, soc=76)
    r = await drv.async_read(client)
    assert (r.voltage, r.soc, r.temperature) == (12.68, 76, None)
    assert client.writes == []  # BM2 needs no commands
    assert drv.sensors == frozenset({"voltage", "soc"})


async def test_bm2_silent(monkeypatch):
    monkeypatch.setattr(leagend, "BM2_STREAM_TIMEOUT", 0.3)
    client = bm2_device(interval=10)
    client.start_notify = lambda char, cb: __import__("asyncio").sleep(0)
    with pytest.raises(DriverError, match="no BM2 reading"):
        await LeagendDriver("BM2").async_read(client)
