# BLE Battery Monitor for Home Assistant

One integration for cheap Bluetooth 12 V battery monitors, built on Home Assistant's
Bluetooth stack so it works through ESPHome Bluetooth proxies.

| Family | Models | Status | Readings |
|---|---|---|---|
| KONNWEI | BK400 | live-tested | voltage, temperature, estimated SoC |
| KONNWEI | BK300 | untested (same framing, uses the `0B0B` voltage poll) | voltage, temperature if supported |
| Leagend | BM6 | tested in simulation, live test pending | voltage, temperature, state of charge, state |
| Leagend | BM2 ("Battery Monitor", ZX-1689) | live-tested | voltage, state of charge (no temperature sensor) |
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

### Safety

Drivers only send what the vendor apps send on every connect. They never send firmware-update,
data-delete, rename or factory/calibration commands. On a KONNWEI device that reports firmware-update
mode, the driver stops after the first query.

## Install (manual)

Copy `custom_components/ble_battery_monitor` into your HA `config/custom_components/`, restart, then
add **BLE Battery Monitor** (or accept the discovery). You need a connectable adapter or an ESPHome
proxy with `bluetooth_proxy: active: true` in range.

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
