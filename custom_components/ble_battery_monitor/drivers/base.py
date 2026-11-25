"""Driver interface shared by all monitor families.

Drivers must not import Home Assistant: they get plain advertisement fields for
detection and a connected bleak client for reading, so they can be unit tested
against a simulated device.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
import time
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from bleak import BleakClient
    from bleak.backends.characteristic import BleakGATTCharacteristic


class DriverError(Exception):
    """A read failed in a way that is the device's or protocol's fault."""


class CharacteristicMissing(DriverError):
    """Expected GATT service/characteristic not found (stale cache or wrong device)."""


@dataclass(frozen=True, slots=True)
class Advertisement:
    """The advertisement fields a driver may use for detection."""

    address: str
    name: str | None
    manufacturer_data: dict[int, bytes]
    service_uuids: list[str]


@dataclass(slots=True)
class Reading:
    """One poll's worth of data. Fields a device does not report stay None."""

    voltage: float | None = None
    temperature: float | None = None
    soc: int | None = None
    state: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HistorySample:
    """One logged sample read back from the device's own memory.

    ts is a unix timestamp. Leagend devices don't store timestamps, so theirs
    are estimated from the newest record and the fixed logging interval.
    """

    ts: float
    voltage: float | None = None
    soc: int | None = None
    temperature: float | None = None


@dataclass(slots=True)
class DeviceDetails:
    """Static-ish details for the device registry."""

    manufacturer: str
    model: str
    sw_version: str | None = None
    hw_version: str | None = None


class Driver(ABC):
    """Base class for a monitor family."""

    key: ClassVar[str]
    manufacturer: ClassVar[str]
    #: Reading fields this driver can fill; only these get entities.
    sensors: frozenset[str]
    #: True if async_read_history is implemented for this model.
    supports_history: bool = False

    def __init__(self, model: str) -> None:
        """Initialize for a detected model."""
        self.details = DeviceDetails(self.manufacturer, model)
        #: Recent raw traffic (direction, hex) for diagnostics.
        self.trace: deque[tuple[float, str, str]] = deque(maxlen=60)
        #: Summary of the last history download, for diagnostics.
        self.history_info: dict[str, Any] | None = None

    @classmethod
    @abstractmethod
    def detect(cls, adv: Advertisement) -> str | None:
        """Return the model name if this advertisement belongs to this family."""

    @abstractmethod
    async def async_read(self, client: BleakClient) -> Reading:
        """Read current values from a connected client. Must not hang."""

    async def async_read_history(self, client: BleakClient, since: float) -> list[HistorySample]:
        """Read logged samples newer than about `since` (unix time). Must not hang.

        Called on an already-connected client, after async_read. May return
        older samples too; the caller drops what it already has.
        """
        raise NotImplementedError

    def _trace(self, direction: str, data: bytes) -> None:
        self.trace.append((round(time.time(), 3), direction, data.hex()))


def find_char(
    client: BleakClient, service_fragment: str, *, want: str, prefer: str | None = None
) -> BleakGATTCharacteristic:
    """Find a characteristic in the first service whose UUID contains service_fragment.

    want: a bleak property name ("notify", "write", "write-without-response").
    prefer: a UUID fragment to pick first among candidates.
    """
    service = next((s for s in client.services if service_fragment in str(s.uuid).lower()), None)
    if service is None:
        raise CharacteristicMissing(f"service *{service_fragment}* not found")
    candidates = [c for c in service.characteristics if want in c.properties]
    if prefer:
        candidates.sort(key=lambda c: prefer not in str(c.uuid).lower())
    if not candidates:
        raise CharacteristicMissing(f"no '{want}' characteristic in {service.uuid}")
    return candidates[0]
