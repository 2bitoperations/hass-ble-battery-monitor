# BLE Battery Monitor for Home Assistant

One integration for cheap Bluetooth 12 V battery monitors, built on Home Assistant's
Bluetooth stack so it works through ESPHome Bluetooth proxies.

| Family | Models | Status | Readings | History backfill |
|---|---|---|---|---|
| KONNWEI | BK400 | live-tested | voltage, temperature, estimated SoC | voltage, estimated SoC (≤ 24 h, 5 min) |
| KONNWEI | BK300 | untested (same framing, uses the `0B0B` voltage poll) | voltage, temperature if supported | no |
| Leagend | BM6 | tested in simulation, live test pending | voltage, temperature, state of charge, state | voltage, SoC, temperature (~48 h) |
| Leagend | BM2 ("Battery Monitor", ZX-1689) | live-tested | voltage, state of charge (no temperature sensor) | voltage |
| Leagend | BM7 | not yet | |

## How it works

- **Discovery:** precise advert matchers.
  - KONNWEI: service FFF0, plus manufacturer data that spells the device's own MAC.
  - BM6 / BM2: their iBeacon UUIDs or manufacturer-data prefixes, a `BM6*` name, or a BM2 name together with FFF0.

  It doesn't claim every FFF0 device.
- **Polling:** advertisement-triggered. When the monitor is heard and a poll is due, it connects,
  reads, and **disconnects**, so a proxy connection slot is held for a few seconds, not forever.
  Default interval 120 s (Options: 30–3600 s); after a failure it retries sooner (60 s).
- **Hard timeouts everywhere:** a poll can't hang (45 s overall budget; per-reply timeouts inside).
- **No extra requirements:** AES for the BM6 uses `cryptography`, which ships with HA.
- **Availability:** the last reading stays shown through short dropouts (3 poll intervals, minimum
  15 min), then goes unavailable. Errors are logged once per outage, not every minute.
- **Diagnostics:** the download includes the last reading, the poll error and the last ~60 raw BLE frames.

### History backfill

BM2 and BM6 monitors keep their own log (one record every 2 minutes; the BM2 sends all of it, ~70 days
/ 200 KB, ~40 s through a proxy). The BK400 only logs inside a schedule, so with backfill on, this
integration keeps a rolling 24 h schedule on it (a sample every 5 minutes, 288 of its 20480 slots;
renewed while in range, replacing any schedules set in the KONNWEI app).

When a monitor comes back after being out of range (more than 10 minutes without a good reading), or
on the first poll after Home Assistant starts, the poll also downloads that log and imports the
missing hours into the sensors' **long-term statistics** (hourly mean/min/max). The BK400's log is
cleared after a verified (CRC-checked) download and logging restarts.

- Only hours with no statistics yet are filled, so live data is never overwritten.
- Home Assistant can't add states to the past, so a backfilled gap shows in a **Statistics graph** card
  (and in history graphs before the first recorded state), not as states in the history graph.
- Leagend records carry no timestamps; they're dated back from the newest one in 2-minute steps.
- Turn it off in Options, or press the **Fetch history** button to backfill on demand (up to 7 days,
  limited by what the device keeps).

### Safety

Drivers only send what the vendor apps send on every connect (plus the history download the apps use).
They never send firmware-update,
data-delete, rename or factory/calibration commands. On a KONNWEI device that reports firmware-update
mode, the driver stops after the first query.

### Research service

`ble_battery_monitor.konnwei_command` connects to a KONNWEI monitor, sends a list of commands
(`"0B09"`, `"0B02:<payload hex>"`, ...) and returns the raw notifications. Only status, info, time-sync,
LED and data-logging commands are accepted; firmware, delete, rename and factory commands are refused.

## Install

You need a connectable Bluetooth adapter, or an ESPHome proxy with `bluetooth_proxy: active: true`, in
range of the monitor.

**HACS (custom repository; this integration isn't in the HACS default list):** go to HACS → ⋮ →
*Custom repositories*, add `https://github.com/2bitoperations/hass-ble-battery-monitor` as type
**Integration**, then download **BLE Battery Monitor** and restart Home Assistant. Monitors in range
show up as discovered devices, or you can add them from **Add integration → BLE Battery Monitor**.

**Manual:** copy `custom_components/ble_battery_monitor` from the latest
[release](https://github.com/2bitoperations/hass-ble-battery-monitor/releases) into
`config/custom_components/` and restart.

## Releases

Every push to `main` that passes CI (tests, ruff, hassfest, HACS validation) is released automatically.
The workflow bumps the patch version in `manifest.json`, tags `vX.Y.Z`, and publishes a GitHub release,
which HACS offers as an update. To skip a release, put `[skip release]` in the commit message.

## Development

```
uv venv -p 3.14 && uv pip install pytest-homeassistant-custom-component bleak-retry-connector habluetooth \
  bluetooth-adapters bluetooth-auto-recovery bluetooth-data-tools dbus-fast aiousbwatcher serialx
uv run pytest -q
uvx ruff check . && uvx ruff format --check .
```

Drivers (`drivers/`) don't import Home Assistant, and are tested against simulated devices in
`tests/fakes.py`. To add a family: subclass `drivers.base.Driver` (`detect()` + `async_read()`),
register it in `drivers/__init__.py`, and add a manifest matcher.

## Estimated state of charge

Monitors that don't report SoC (KONNWEI BK series) get an **Estimated state of charge** sensor, worked out
from resting voltage. Set the battery type (flooded / AGM / gel / LiFePO4 / none) and the nominal voltage
(6–48 V) in Options. It's only accurate after the battery has rested for a few hours: it reads high while
charging and low under load. LiFePO4's flat voltage curve makes it a coarse guide only. A small lead-acid
temperature term (+1.2 mV/°C at 12 V, about 1 % per 10 °C) is applied when the monitor reports temperature.
