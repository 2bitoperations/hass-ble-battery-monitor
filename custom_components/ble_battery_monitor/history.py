"""Backfill: import samples from the device's own log into long-term statistics.

Home Assistant can't insert states into the past, but it can import hourly
statistics (mean/min/max) for a sensor. Those show in statistics graphs and in
history graphs before the first recorded state.

Only hours that have no statistics row yet are imported. Hours with live
readings (or the stale value shown during a short dropout) already have one, so
live data is never overwritten and a repeated download is harmless.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import PERCENTAGE, UnitOfElectricPotential, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    ElectricPotentialConverter,
    TemperatureConverter,
)

from .const import DOMAIN
from .drivers import HistorySample

_LOGGER = logging.getLogger(__name__)

HOUR = timedelta(hours=1)

#: sensor key -> (native unit, converter or None)
UNITS: dict[str, tuple[str, type[BaseUnitConverter] | None]] = {
    "voltage": (UnitOfElectricPotential.VOLT, ElectricPotentialConverter),
    "temperature": (UnitOfTemperature.CELSIUS, TemperatureConverter),
    "soc": (PERCENTAGE, None),
    "soc_estimate": (PERCENTAGE, None),
}

ValueFn = Callable[[HistorySample], float | None]


def bucket_by_hour(
    samples: Iterable[HistorySample], values: dict[str, ValueFn], before: datetime
) -> dict[str, dict[datetime, list[float]]]:
    """Group sample values into UTC hours that end at or before `before`."""
    out: dict[str, dict[datetime, list[float]]] = {k: defaultdict(list) for k in values}
    for sample in samples:
        hour = datetime.fromtimestamp(sample.ts, UTC).replace(minute=0, second=0, microsecond=0)
        if hour + HOUR > before:
            continue  # the current hour is still being recorded live
        for key, fn in values.items():
            if (v := fn(sample)) is not None:
                out[key][hour].append(float(v))
    return out


async def async_import_history(
    hass: HomeAssistant,
    address: str,
    samples: list[HistorySample],
    values: dict[str, ValueFn],
) -> dict[str, int]:
    """Import hourly statistics for hours missing from the recorder.

    values maps a sensor key to a function that pulls its value out of a sample.
    Returns the number of hours imported per sensor key.
    """
    if "recorder" not in hass.config.components:
        return {}
    registry = er.async_get(hass)
    recorder = get_instance(hass)
    buckets = bucket_by_hour(samples, values, dt_util.utcnow())
    imported: dict[str, int] = {}
    for key, hours in buckets.items():
        if not hours:
            continue
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{address}_{key}")
        if entity_id is None:
            continue
        start, end = min(hours), max(hours) + HOUR
        existing = await recorder.async_add_executor_job(
            statistics_during_period, hass, start, end, {entity_id}, "hour", None, {"mean"}
        )
        have = {row["start"] for row in existing.get(entity_id, [])}
        missing = sorted(h for h in hours if h.timestamp() not in have)
        if not missing:
            imported[key] = 0
            continue
        metadata, convert = await _async_metadata(hass, entity_id, key)
        stats = [
            StatisticData(
                start=h,
                mean=round(convert(sum(hours[h]) / len(hours[h])), 3),
                min=round(convert(min(hours[h])), 3),
                max=round(convert(max(hours[h])), 3),
            )
            for h in missing
        ]
        async_import_statistics(hass, metadata, stats)
        imported[key] = len(stats)
        _LOGGER.debug("%s: imported %d hours of %s", address, len(stats), entity_id)
    return imported


async def _async_metadata(
    hass: HomeAssistant, entity_id: str, key: str
) -> tuple[StatisticMetaData, Callable[[float], float]]:
    """Reuse the sensor's own statistics metadata (and unit) when it exists."""
    native_unit, converter = UNITS[key]
    found = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={entity_id})
    )
    if entity_id in found:
        meta: dict[str, Any] = dict(found[entity_id][1])
        meta.pop("has_mean", None)
        unit = meta.get("unit_of_measurement")
        if converter is not None and unit and unit != native_unit:
            return StatisticMetaData(**meta), lambda v: converter.convert(v, native_unit, unit)
        return StatisticMetaData(**meta), lambda v: v
    return (
        StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=converter.UNIT_CLASS if converter else None,
            unit_of_measurement=native_unit,
        ),
        lambda v: v,
    )
