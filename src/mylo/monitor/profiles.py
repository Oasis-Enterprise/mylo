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

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mylo.logging_setup import get_logger

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
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(profile_set.model_dump()), encoding="utf-8")
        except OSError as exc:
            log.warning("profiles.save_failed", error=str(exc))
