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

import pytest

from mylo.monitor.profiles import (
    AWAY_CONFIDENCE_FULL_SAMPLES,
    EntityProfile,
    ProfileSet,
    ProfileStore,
    away_confidence,
    away_eligible,
    confidence,
    duration_eligible,
    duration_threshold_s,
    fold_transitions,
    is_rarely_on_while_away,
    p95_duration_s,
)
from mylo.monitor.transitions import Transition


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


def _t(entity_id: str, from_state: str, to_state: str, ts: str) -> Transition:
    return Transition(entity_id=entity_id, from_state=from_state, to_state=to_state, timestamp=ts)


def test_fold_records_completed_cycle() -> None:
    ps = ProfileSet()
    cycles = fold_transitions(
        ps,
        [
            _t("light.kitchen", "off", "on", "2026-06-01T06:30:00+00:00"),
            _t("light.kitchen", "on", "off", "2026-06-01T07:00:00+00:00"),
        ],
    )
    assert cycles == 1
    p = ps.entities["light.kitchen"]
    assert p.cycle_count == 1
    assert p.max_duration_s == 1800.0
    assert p.duration_histogram[2] == 1  # 30min lands in the ≤30m bucket
    assert p.days_observed == 1
    assert p.total_events == 2
    assert ps.last_folded == "2026-06-01T07:00:00+00:00"


def test_fold_watermark_prevents_double_counting() -> None:
    ps = ProfileSet()
    transitions = [
        _t("light.kitchen", "off", "on", "2026-06-01T06:30:00+00:00"),
        _t("light.kitchen", "on", "off", "2026-06-01T07:00:00+00:00"),
    ]
    fold_transitions(ps, transitions)
    cycles = fold_transitions(ps, transitions)  # same data again
    assert cycles == 0
    assert ps.entities["light.kitchen"].cycle_count == 1


def test_fold_cycle_spans_fold_boundary() -> None:
    ps = ProfileSet()
    fold_transitions(ps, [_t("light.kitchen", "off", "on", "2026-06-01T22:00:00+00:00")])
    assert ps.entities["light.kitchen"].active_since == "2026-06-01T22:00:00+00:00"
    cycles = fold_transitions(ps, [_t("light.kitchen", "on", "off", "2026-06-02T01:00:00+00:00")])
    assert cycles == 1
    assert ps.entities["light.kitchen"].max_duration_s == 3 * 3600.0


def test_fold_ignores_unwatched_domains_and_counts_days() -> None:
    ps = ProfileSet()
    fold_transitions(
        ps,
        [
            _t("person.max", "home", "not_home", "2026-06-01T08:00:00+00:00"),
            _t("light.kitchen", "off", "on", "2026-06-01T08:30:00+00:00"),
            _t("light.kitchen", "on", "off", "2026-06-01T09:00:00+00:00"),
            _t("light.kitchen", "off", "on", "2026-06-02T08:30:00+00:00"),
            _t("light.kitchen", "on", "off", "2026-06-02T09:00:00+00:00"),
        ],
    )
    assert "person.max" not in ps.entities
    assert ps.entities["light.kitchen"].days_observed == 2
    assert ps.entities["light.kitchen"].cycle_count == 2


def test_p95_from_histogram() -> None:
    p = EntityProfile(entity_id="light.kitchen")
    # 20 cycles of ~30 min (bucket index 2: ≤1800s), one 6h outlier.
    p.duration_histogram[2] = 20
    p.duration_histogram[6] = 1  # ≤8h bucket
    p.cycle_count = 21
    p.max_duration_s = 21600.0
    # 95th percentile of 21 cycles = the 20th — still in the 30m bucket.
    assert p95_duration_s(p) == 1800.0


def test_p95_empty_histogram_is_zero() -> None:
    assert p95_duration_s(EntityProfile(entity_id="x")) == 0.0


def test_p95_last_bucket_uses_max_duration() -> None:
    p = EntityProfile(entity_id="x")
    p.duration_histogram[9] = 10  # everything >24h
    p.max_duration_s = 100000.0
    assert p95_duration_s(p) == 100000.0


def _confident_profile(**overrides: object) -> EntityProfile:
    p = EntityProfile(entity_id="light.kitchen", days_observed=20, cycle_count=20)
    p.duration_histogram[2] = 20  # twenty ~30min cycles → p95 = 1800s
    p.max_duration_s = 2100.0  # 35 min
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def test_duration_eligibility_gates() -> None:
    assert duration_eligible(_confident_profile())
    assert not duration_eligible(_confident_profile(days_observed=10))
    assert not duration_eligible(_confident_profile(cycle_count=5))


def test_away_eligibility_and_rarity() -> None:
    p = _confident_profile(away_samples=10, on_while_away_samples=1)
    assert away_eligible(p)
    assert is_rarely_on_while_away(p)  # 10% < 20%
    routine = _confident_profile(away_samples=10, on_while_away_samples=8)
    assert not is_rarely_on_while_away(routine)  # 80% — porch light
    assert not away_eligible(_confident_profile(away_samples=3))


def test_confidence_scales_with_days() -> None:
    assert confidence(_confident_profile(days_observed=21)) == 1.0
    assert abs(confidence(_confident_profile(days_observed=7)) - 7 / 21) < 1e-9


def test_duration_threshold_default_margins() -> None:
    p = _confident_profile()
    # max(2100 * 1.25, 1800 * 2.0) = max(2625, 3600) = 3600
    assert duration_threshold_s(p) == 3600.0


def test_duration_threshold_lock_uses_tight_margins() -> None:
    p = EntityProfile(entity_id="lock.front", days_observed=20, cycle_count=20)
    p.duration_histogram[2] = 20
    p.max_duration_s = 2100.0
    # max(2100 * 1.1, 1800 * 1.5) = max(2310, 2700) = 2700
    assert duration_threshold_s(p) == 2700.0


def test_duration_threshold_door_sensor_uses_tight_margins() -> None:
    p = EntityProfile(entity_id="binary_sensor.garage", days_observed=20, cycle_count=20)
    p.duration_histogram[2] = 20
    p.max_duration_s = 2100.0
    assert duration_threshold_s(p, device_class="garage_door") == 2700.0
    assert duration_threshold_s(p, device_class="motion") == 3600.0


def test_fold_malformed_timestamp_does_not_crash() -> None:
    ps = ProfileSet()
    cycles = fold_transitions(
        ps,
        [
            _t("light.kitchen", "off", "on", "not-a-timestamp"),
            _t("light.kitchen", "on", "off", "really-not-a-timestamp"),
        ],
    )
    assert cycles == 0


def test_fold_off_without_prior_on_records_no_cycle() -> None:
    ps = ProfileSet()
    cycles = fold_transitions(ps, [_t("light.kitchen", "on", "off", "2026-06-01T07:00:00+00:00")])
    assert cycles == 0
    assert ps.entities["light.kitchen"].cycle_count == 0


def test_histogram_validator_pads_and_truncates() -> None:
    p = EntityProfile(entity_id="x", duration_histogram=[1, 2], active_hours=[0] * 30)
    assert len(p.duration_histogram) == 10
    assert p.duration_histogram[0] == 1
    assert len(p.active_hours) == 24


def test_away_confidence_scales_with_samples() -> None:
    # 0 samples → 0.0; 8 → 0.5 (half of AWAY_CONFIDENCE_FULL_SAMPLES=16);
    # 16 → 1.0; 20 (over cap) → still 1.0.
    assert away_confidence(EntityProfile(entity_id="x", away_samples=0)) == 0.0
    assert away_confidence(EntityProfile(entity_id="x", away_samples=8)) == pytest.approx(
        8 / AWAY_CONFIDENCE_FULL_SAMPLES
    )
    assert away_confidence(EntityProfile(entity_id="x", away_samples=16)) == 1.0
    assert away_confidence(EntityProfile(entity_id="x", away_samples=20)) == 1.0
