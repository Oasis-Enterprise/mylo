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

"""Tests for learned entity profiles: store roundtrip, folding,
gates, and thresholds."""

from __future__ import annotations

from pathlib import Path

from mylo.monitor.profiles import (
    EntityProfile,
    ProfileSet,
    ProfileStore,
)


def test_profile_store_roundtrip(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    profile_set = ProfileSet(
        last_folded="2026-06-01T00:00:00+00:00",
        entities={
            "light.kitchen": EntityProfile(
                entity_id="light.kitchen",
                days_observed=15,
                cycle_count=20,
                max_duration_s=21600.0,
            )
        },
    )
    store.save(profile_set)

    loaded = store.load()
    assert loaded.last_folded == "2026-06-01T00:00:00+00:00"
    assert loaded.entities["light.kitchen"].cycle_count == 20
    assert loaded.entities["light.kitchen"].max_duration_s == 21600.0


def test_profile_store_missing_file_returns_empty(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    loaded = store.load()
    assert loaded.entities == {}
    assert loaded.last_folded is None


def test_profile_store_corrupt_file_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "profiles.json").write_text("{not valid json", encoding="utf-8")
    store = ProfileStore(tmp_path)
    loaded = store.load()
    assert loaded.entities == {}


def test_profile_defaults() -> None:
    p = EntityProfile(entity_id="switch.fan")
    assert p.days_observed == 0
    assert p.cycle_count == 0
    assert len(p.duration_histogram) == 10
    assert len(p.active_hours) == 24
    assert p.away_samples == 0
