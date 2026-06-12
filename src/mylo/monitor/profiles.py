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

"""Per-entity learned behavior profiles.

The transition log (``transitions.jsonl``) holds 14 days of raw
state changes. Profiles are the compact, long-lived aggregates
folded out of that stream: how long an entity typically stays on,
the longest it has EVER stayed on, and how it behaves when nobody
is home. Detectors gate on these profiles — an entity earns the
right to alert by accumulating observations ("quiet until
confident").

Stored in ``{mylo_data_dir}/profiles.json`` — deliberately NOT in
``context.yaml``: this is machine state, and corruption is never
fatal (entities just re-enter the quiet period).
"""

from __future__ import annotations

import contextlib
import json
import math
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from mylo.files.manager import atomic_write
from mylo.logging_setup import get_logger
from mylo.monitor.transitions import Transition

log = get_logger(__name__)

# Duration histogram bucket upper bounds, in seconds:
# 5m, 15m, 30m, 1h, 2h, 4h, 8h, 16h, 24h — plus one open-ended >24h
# bucket. Coarse on purpose: enough to derive a p95 without ever
# storing raw durations.
BUCKET_EDGES_S: tuple[int, ...] = (300, 900, 1800, 3600, 7200, 14400, 28800, 57600, 86400)
NUM_BUCKETS = len(BUCKET_EDGES_S) + 1

# The state that starts a duration cycle, per domain. Domains not
# listed here (person, climate) get no duration tracking.
ACTIVE_STATES: dict[str, str] = {
    "light": "on",
    "switch": "on",
    "fan": "on",
    "media_player": "on",
    "lock": "unlocked",
    "cover": "open",
    "binary_sensor": "on",
}

# Eligibility gates — quiet until confident.
MIN_DAYS_FOR_DURATION = 14
MIN_CYCLES_FOR_DURATION = 8
MIN_AWAY_SAMPLES = 8
AWAY_RARE_FRACTION = 0.20
CONFIDENCE_FULL_DAYS = 21

# Duration must exceed BOTH the all-time max (with margin) and a
# multiple of the typical (p95) duration. Locks and doors get
# tighter margins — higher stakes.
DEFAULT_MARGINS = (1.25, 2.0)  # (max multiplier, p95 multiplier)
TIGHT_MARGINS = (1.1, 1.5)
TIGHT_DEVICE_CLASSES = frozenset({"door", "garage_door"})


class EntityProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    entity_id: str
    first_seen: str | None = None
    last_event_date: str | None = None  # YYYY-MM-DD of last counted event
    days_observed: int = 0
    total_events: int = 0
    cycle_count: int = 0
    max_duration_s: float = 0.0
    duration_histogram: list[int] = Field(default_factory=lambda: [0] * NUM_BUCKETS)
    active_hours: list[int] = Field(default_factory=lambda: [0] * 24)
    # Entered the active state but the cycle hasn't closed yet —
    # carries across fold runs so boundary-spanning cycles count.
    active_since: str | None = None
    away_samples: int = 0
    on_while_away_samples: int = 0

    @field_validator("duration_histogram", "active_hours", mode="after")
    @classmethod
    def _normalize_length(cls, v: list[int], info: ValidationInfo) -> list[int]:
        expected = NUM_BUCKETS if info.field_name == "duration_histogram" else 24
        if len(v) < expected:
            return v + [0] * (expected - len(v))
        return v[:expected]


class ProfileSet(BaseModel):
    """Root of profiles.json: all profiles + the fold watermark."""

    model_config = ConfigDict(extra="allow")

    last_folded: str | None = None
    entities: dict[str, EntityProfile] = Field(default_factory=dict)


class ProfileStore:
    """Load/save profiles.json. Corruption is never fatal."""

    def __init__(self, mylo_data_dir: Path) -> None:
        self._path = mylo_data_dir / "profiles.json"

    def load(self) -> ProfileSet:
        if not self._path.exists():
            return ProfileSet()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return ProfileSet.model_validate(raw)
        except (OSError, ValueError) as exc:
            log.warning("profiles.load_failed", error=str(exc))
            return ProfileSet()

    def save(self, profile_set: ProfileSet) -> None:
        # A torn write would silently discard weeks of accumulated learning
        # that the 14-day raw log can't rebuild.
        try:
            atomic_write(self._path, json.dumps(profile_set.model_dump()))
        except OSError as exc:
            log.warning("profiles.save_failed", error=str(exc))


def fold_transitions(profile_set: ProfileSet, transitions: list[Transition]) -> int:
    """Fold new transitions into the profiles, in place.

    Only transitions newer than the watermark (``last_folded``) are
    processed, so re-reading the same 14-day window every night
    never double-counts. Returns the number of cycles recorded.
    """
    watermark = profile_set.last_folded or ""
    # Strict `>` means an event logged in the same second as the watermark but
    # appended after the nightly read is knowingly dropped — bounded loss: at
    # most one cycle, and a dangling active_since self-heals on the next activation.
    fresh = sorted(
        (t for t in transitions if t.timestamp > watermark),
        key=lambda t: t.timestamp,
    )
    cycles = 0

    for t in fresh:
        domain = t.entity_id.split(".", 1)[0]
        active = ACTIVE_STATES.get(domain)
        if active is None:
            continue

        profile = profile_set.entities.get(t.entity_id)
        if profile is None:
            profile = EntityProfile(entity_id=t.entity_id, first_seen=t.timestamp)
            profile_set.entities[t.entity_id] = profile

        profile.total_events += 1
        event_date = t.timestamp[:10]
        if event_date != profile.last_event_date:
            profile.days_observed += 1
            profile.last_event_date = event_date
        with contextlib.suppress(ValueError):
            profile.active_hours[datetime.fromisoformat(t.timestamp).hour] += 1

        if t.to_state == active:
            profile.active_since = t.timestamp
        elif t.from_state == active and profile.active_since is not None:
            try:
                start = datetime.fromisoformat(profile.active_since)
                end = datetime.fromisoformat(t.timestamp)
            except ValueError:
                profile.active_since = None
                continue
            profile.active_since = None
            duration_s = (end - start).total_seconds()
            if duration_s > 0:
                _record_cycle(profile, duration_s)
                cycles += 1

    if fresh:
        profile_set.last_folded = fresh[-1].timestamp
    return cycles


def _record_cycle(profile: EntityProfile, duration_s: float) -> None:
    profile.cycle_count += 1
    profile.max_duration_s = max(profile.max_duration_s, duration_s)
    profile.duration_histogram[_bucket_index(duration_s)] += 1


def _bucket_index(duration_s: float) -> int:
    for i, edge in enumerate(BUCKET_EDGES_S):
        if duration_s <= edge:
            return i
    return len(BUCKET_EDGES_S)


def p95_duration_s(profile: EntityProfile) -> float:
    """95th-percentile cycle duration — upper bound of its bucket.

    The open-ended last bucket uses the exact observed max instead.
    """
    total = sum(profile.duration_histogram)
    if total == 0:
        return 0.0
    target = math.ceil(total * 0.95)
    cumulative = 0
    for i, count in enumerate(profile.duration_histogram):
        cumulative += count
        if cumulative >= target:
            if i < len(BUCKET_EDGES_S):
                return float(BUCKET_EDGES_S[i])
            return profile.max_duration_s
    return profile.max_duration_s


def duration_eligible(profile: EntityProfile) -> bool:
    """Quiet until confident: enough days AND enough cycles."""
    return (
        profile.days_observed >= MIN_DAYS_FOR_DURATION
        and profile.cycle_count >= MIN_CYCLES_FOR_DURATION
    )


def away_eligible(profile: EntityProfile) -> bool:
    return profile.away_samples >= MIN_AWAY_SAMPLES


def is_rarely_on_while_away(profile: EntityProfile) -> bool:
    """True when being on during away time is unusual for this entity."""
    if profile.away_samples == 0:
        return False
    return profile.on_while_away_samples / profile.away_samples < AWAY_RARE_FRACTION


def confidence(profile: EntityProfile) -> float:
    """0..1 — orders findings in the banner, never shown as a number."""
    return min(1.0, profile.days_observed / CONFIDENCE_FULL_DAYS)


def duration_threshold_s(profile: EntityProfile, *, device_class: str | None = None) -> float:
    """Seconds in the active state beyond which a finding fires.

    Must beat the all-time max with margin AND a multiple of the
    typical (p95) duration — whichever is larger governs.

    Callers must check ``duration_eligible`` first; an empty profile returns 0.0, which every duration exceeds.
    """
    domain = profile.entity_id.split(".", 1)[0]
    tight = domain == "lock" or (domain == "binary_sensor" and device_class in TIGHT_DEVICE_CLASSES)
    margin_max, margin_p95 = TIGHT_MARGINS if tight else DEFAULT_MARGINS
    return max(profile.max_duration_s * margin_max, p95_duration_s(profile) * margin_p95)
