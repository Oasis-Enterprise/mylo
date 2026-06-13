# Copyright 2026 Maxwell Monson / Oasis Enterprise LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Behavioral pattern detection from state transition history.

Analyzes the transition log to find recurring time-of-day behaviors:

* "light.kitchen turns off between 22:45-23:15 on weekdays"
* "person.maxwell arrives home between 17:30-18:15 on weekdays"
* "lock.front_door locked between 22:00-23:00 daily"

These feed the suggestion engine: when a pattern *doesn't* happen
at the expected time, Mylo can proactively ask if the user forgot.

The detector runs nightly (after baselines) and stores discovered
patterns in ``context.yaml → patterns`` using the existing Pattern
schema.

Algorithm:
1. Read 14 days of transitions for each entity.
2. Group transitions by (entity_id, to_state).
3. For each group, cluster the timestamps by hour-of-day.
4. If a cluster has ≥5 occurrences within a ±30min window across
   different days, it's a behavioral pattern.
5. Compute confidence = occurrences / days_in_window.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime

from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile, Pattern
from mylo.monitor.transitions import TransitionLogger

log = get_logger(__name__)

# Minimum occurrences to consider something a pattern.
MIN_OCCURRENCES = 5

# Maximum spread in minutes for a time-of-day cluster.
CLUSTER_WINDOW_MINUTES = 30

# Minimum confidence to store a pattern (occurrences / days observed).
MIN_CONFIDENCE = 0.4


def detect_patterns(
    transition_logger: TransitionLogger,
    memory: MemoryFile,
    *,
    days: int = 14,
) -> list[Pattern]:
    """Analyze transition history and return newly-discovered patterns.

    Only returns patterns not already stored in memory. Existing
    patterns get their confidence updated in place.
    """
    transitions = transition_logger.read_recent(hours=days * 24)
    if not transitions:
        return []

    # Group by (entity_id, to_state) → list of timestamps.
    groups: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for t in transitions:
        try:
            dt = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        groups[(t.entity_id, t.to_state)].append(dt)

    now = datetime.now(UTC)
    existing_ids = {p.id for p in memory.patterns}
    new_patterns: list[Pattern] = []

    for (entity_id, to_state), timestamps in groups.items():
        if len(timestamps) < MIN_OCCURRENCES:
            continue

        clusters = _cluster_by_time_of_day(timestamps)

        for cluster in clusters:
            if len(cluster) < MIN_OCCURRENCES:
                continue

            # How many unique days does this cluster span?
            unique_days = len({dt.date() for dt in cluster})
            confidence = unique_days / days

            if confidence < MIN_CONFIDENCE:
                continue

            avg_minute = _average_minute_of_day(cluster)
            hour = avg_minute // 60
            minute = avg_minute % 60
            spread = _spread_minutes(cluster)

            # Bucket the time into 30-min slots for the id. The averaged
            # minute drifts a little each night as the 14-day window
            # slides; without bucketing, every drift would mint a brand
            # new pattern id and the list would grow without bound. The
            # description still carries the precise time.
            pattern_id = f"behavior_{entity_id}_{to_state}_{_time_slot(avg_minute)}"

            # Update existing pattern confidence if present.
            existing = _find_pattern(memory, pattern_id)
            if existing:
                existing.confidence = round(confidence, 2)
                existing.last_confirmed = now.isoformat(timespec="seconds")
                continue

            if pattern_id in existing_ids:
                continue

            # Determine day pattern.
            weekday_count = sum(1 for dt in cluster if dt.weekday() < 5)
            weekend_count = sum(1 for dt in cluster if dt.weekday() >= 5)
            total = len(cluster)
            if weekday_count > 0.8 * total:
                day_label = "weekdays"
            elif weekend_count > 0.8 * total:
                day_label = "weekends"
            else:
                day_label = "daily"

            friendly_time = f"{hour:02d}:{minute:02d}"
            description = (
                f"{entity_id} → {to_state} around {friendly_time} "
                f"({day_label}, ±{spread}min, "
                f"{unique_days}/{days} days observed)"
            )

            new_patterns.append(
                Pattern(
                    id=pattern_id,
                    description=description,
                    confidence=round(confidence, 2),
                    first_observed=min(cluster).isoformat(timespec="seconds"),
                    last_confirmed=now.isoformat(timespec="seconds"),
                    source="behavioral",
                )
            )

    if new_patterns:
        log.info("behavioral.patterns_detected", count=len(new_patterns))

    return new_patterns


def _cluster_by_time_of_day(
    timestamps: list[datetime],
) -> list[list[datetime]]:
    """Group timestamps into clusters by time-of-day similarity.

    Uses a simple sorted-sweep: sort by minute-of-day, then walk
    and split when the gap exceeds CLUSTER_WINDOW_MINUTES.
    """
    # Convert to (minute_of_day, original_dt) pairs.
    annotated = [(dt.hour * 60 + dt.minute, dt) for dt in timestamps]
    annotated.sort(key=lambda x: x[0])

    clusters: list[list[datetime]] = []
    current: list[datetime] = []
    current_center: int = -1

    for minute, dt in annotated:
        if not current:
            current.append(dt)
            current_center = minute
        elif abs(minute - current_center) <= CLUSTER_WINDOW_MINUTES:
            current.append(dt)
        else:
            clusters.append(current)
            current = [dt]
            current_center = minute

    if current:
        clusters.append(current)

    # Handle midnight wrap: if the first and last clusters are within
    # CLUSTER_WINDOW_MINUTES of each other across midnight, merge them.
    if len(clusters) >= 2:
        first_center = _average_minute_of_day(clusters[0])
        last_center = _average_minute_of_day(clusters[-1])
        if (1440 - last_center + first_center) <= CLUSTER_WINDOW_MINUTES:
            clusters[0] = clusters[-1] + clusters[0]
            clusters.pop()

    return clusters


def _time_slot(avg_minute: int) -> str:
    """Bucket a minute-of-day into a stable 30-min slot string (e.g.
    "2230"). Keeps a behavior's pattern id stable across the small
    night-to-night drift of its averaged time."""
    bucket = (avg_minute // 30) * 30
    return f"{bucket // 60:02d}{bucket % 60:02d}"


def _average_minute_of_day(timestamps: list[datetime]) -> int:
    """Average minute-of-day, handling midnight wrap with circular mean."""
    if not timestamps:
        return 0

    # Use circular statistics to handle midnight wrap.
    sin_sum = sum(math.sin(2 * math.pi * (dt.hour * 60 + dt.minute) / 1440) for dt in timestamps)
    cos_sum = sum(math.cos(2 * math.pi * (dt.hour * 60 + dt.minute) / 1440) for dt in timestamps)
    avg_angle = math.atan2(sin_sum / len(timestamps), cos_sum / len(timestamps))
    avg_minute = int((avg_angle / (2 * math.pi)) * 1440) % 1440
    return avg_minute


def _spread_minutes(timestamps: list[datetime]) -> int:
    """How many minutes of spread in the cluster (max - min)."""
    minutes = [dt.hour * 60 + dt.minute for dt in timestamps]
    if not minutes:
        return 0
    spread = max(minutes) - min(minutes)
    # Handle midnight wrap.
    if spread > 720:
        spread = 1440 - spread
    return spread


def _find_pattern(memory: MemoryFile, pattern_id: str) -> Pattern | None:
    for p in memory.patterns:
        if p.id == pattern_id:
            return p
    return None
