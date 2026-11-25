"""Constants for BLE Battery Monitor."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ble_battery_monitor"

CONF_DRIVER: Final = "driver"
CONF_MODEL: Final = "model"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_CHEMISTRY: Final = "chemistry"
CONF_NOMINAL_VOLTAGE: Final = "nominal_voltage"

DEFAULT_CHEMISTRY: Final = "flooded"
DEFAULT_NOMINAL_VOLTAGE: Final = 12

DEFAULT_POLL_INTERVAL: Final = 120  # seconds
MIN_POLL_INTERVAL: Final = 30
MAX_POLL_INTERVAL: Final = 3600
#: Retry sooner than the normal interval after a failed poll.
RETRY_INTERVAL: Final = 60

#: How often to check whether a poll is due (adverts alone aren't enough).
POLL_TIMER: Final = 30

#: Whole connect+read budget; a poll never blocks longer than this.
POLL_TIMEOUT: Final = 45
CONNECT_ATTEMPTS: Final = 3

#: Keep showing the last reading for this many poll intervals of failures
#: (at least STALE_MIN seconds) before going unavailable.
STALE_INTERVALS: Final = 3
STALE_MIN: Final = 900

CONF_HISTORY: Final = "history"
DEFAULT_HISTORY: Final = True
#: A gap this long between good readings triggers a history download (backfill).
HISTORY_GAP: Final = 600
#: How far back to backfill after a restart or a manual request.
HISTORY_MAX_AGE: Final = 7 * 86400
#: After a failed history download, wait this long before trying again.
HISTORY_RETRY: Final = 1800
#: Budget for one history download (on top of the normal poll).
HISTORY_TIMEOUT: Final = 150
