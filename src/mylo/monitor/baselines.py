"""Nightly baseline recompute from HA long-term statistics.

HA's ``recorder/statistics_during_period`` websocket command returns
hourly/daily aggregated statistics for entities that the recorder
tracks. We pull the last 7 days, compute mean + stddev for each
monitored entity, and store the result in
``context.yaml → baselines → entities``.

These baselines feed :mod:`monitor.anomaly` — a z-score spike means
the value deviated far enough from "normal" to warrant a notification.

Only numeric sensors (``sensor.*`` with ``state_class: measurement``)
produce meaningful statistics. Entities without recorded stats are
silently skipped.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from mylo.ha.ws_client import CommandError, HaWsClient
from mylo.logging_setup import get_logger
from mylo.memory.schema import Baselines, EntityBaseline, MemoryFile

log = get_logger(__name__)

STATS_WINDOW_DAYS = 7


async def recompute_baselines(
    *,
    ws_client: HaWsClient,
    memory: MemoryFile,
) -> Baselines | None:
    """Pull stats for monitored_entities and compute baselines.

    Returns a new :class:`Baselines` with updated entity entries, or
    None if there's nothing to compute (no monitored entities, or HA
    doesn't have stats for any of them).
    """
    monitored = memory.monitored_entities
    if not monitored:
        log.debug("baselines.skip_no_monitored")
        return None

    # Only request stats for sensor-domain entities — other domains
    # rarely have meaningful long-term statistics.
    sensor_ids = [eid for eid in monitored if eid.startswith("sensor.")]
    if not sensor_ids:
        log.debug("baselines.skip_no_sensors")
        return None

    now = datetime.now(UTC)
    start = now - timedelta(days=STATS_WINDOW_DAYS)

    try:
        raw = await ws_client.send_command(
            "recorder/statistics_during_period",
            start_time=start.isoformat(),
            end_time=now.isoformat(),
            statistic_ids=sensor_ids,
            period="hour",
            timeout=30.0,
        )
    except CommandError as exc:
        log.warning("baselines.stats_fetch_failed", error=f"{exc.code}: {exc.message}")
        return None
    except Exception:
        log.exception("baselines.stats_fetch_failed")
        return None

    if not isinstance(raw, dict):
        log.warning("baselines.unexpected_response_type", type=type(raw).__name__)
        return None

    entity_baselines: list[EntityBaseline] = []
    for entity_id in sensor_ids:
        points = raw.get(entity_id)
        if not isinstance(points, list) or not points:
            continue
        values = _extract_mean_values(points)
        if len(values) < 3:
            continue
        avg = sum(values) / len(values)
        variance = sum((v - avg) ** 2 for v in values) / len(values)
        stddev = math.sqrt(variance)
        entity_baselines.append(
            EntityBaseline(
                entity=entity_id,
                metric="mean",
                avg=round(avg, 4),
                stddev=round(stddev, 4),
                last_calculated=now.isoformat(timespec="seconds"),
            )
        )

    if not entity_baselines:
        log.debug("baselines.no_valid_stats")
        return None

    result = Baselines(entities=entity_baselines)
    log.info("baselines.computed", count=len(entity_baselines))
    return result


def _extract_mean_values(points: list[Any]) -> list[float]:
    """Pull the 'mean' field from each stats point, skipping nulls."""
    values: list[float] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        mean = point.get("mean")
        if mean is None:
            continue
        try:
            values.append(float(mean))
        except (TypeError, ValueError):
            continue
    return values
