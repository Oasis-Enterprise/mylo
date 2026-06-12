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

"""Tests for profile-gated learned detectors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from mylo.memory.schema import empty_memory
from mylo.monitor.detectors import run_learned_checks
from mylo.monitor.profiles import EntityProfile, ProfileSet


def _profile(entity_id: str, *, max_s: float = 2100.0, bucket: int = 2, **overrides: object):
    p = EntityProfile(entity_id=entity_id, days_observed=20, cycle_count=20)
    p.duration_histogram[bucket] = 20
    p.max_duration_s = max_s
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def _state(entity_id: str, state: str, *, hours_ago: float = 0.0, attributes=None):
    changed = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": changed,
        "attributes": attributes or {},
    }


def _ws(states: list[dict]) -> AsyncMock:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=states)
    return ws


async def test_duration_anomaly_fires_beyond_learned_threshold() -> None:
    # Typical use ~30min (p95=1800s), max 35min → threshold 3600s. On 4h.
    profiles = ProfileSet(entities={"light.kitchen": _profile("light.kitchen")})
    ws = _ws([_state("light.kitchen", "on", hours_ago=4.0)])

    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert len(actions) == 1
    assert actions[0].type == "duration_anomaly"
    assert actions[0].entity_id == "light.kitchen"
    assert "longest" in actions[0].message


async def test_duration_silent_within_learned_norms() -> None:
    # Routinely on ~8h (p95 bucket ≤8h → 28800s); max 8.2h.
    # Threshold = max(29520 * 1.25, 28800 * 2) = 57600s = 16h. On 9h → silent.
    profiles = ProfileSet(
        entities={"light.office": _profile("light.office", max_s=29520.0, bucket=6)}
    )
    ws = _ws([_state("light.office", "on", hours_ago=9.0)])

    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert actions == []


async def test_duration_silent_when_not_confident() -> None:
    profiles = ProfileSet(entities={"light.new": _profile("light.new", days_observed=5)})
    ws = _ws([_state("light.new", "on", hours_ago=12.0)])

    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert actions == []


async def test_duration_silent_with_no_profile() -> None:
    ws = _ws([_state("light.unknown", "on", hours_ago=12.0)])
    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=ProfileSet())
    assert actions == []


async def test_while_away_fires_only_when_rare() -> None:
    rare = _profile("light.closet", away_samples=10, on_while_away_samples=0)
    routine = _profile("light.porch", away_samples=10, on_while_away_samples=9)
    profiles = ProfileSet(entities={"light.closet": rare, "light.porch": routine})
    ws = _ws(
        [
            _state("person.max", "not_home"),
            _state("light.closet", "on", hours_ago=0.2),
            _state("light.porch", "on", hours_ago=0.2),
        ]
    )

    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    while_away = [a for a in actions if a.type == "while_away"]
    assert [a.entity_id for a in while_away] == ["light.closet"]


async def test_while_away_silent_when_someone_home() -> None:
    rare = _profile("light.closet", away_samples=10, on_while_away_samples=0)
    profiles = ProfileSet(entities={"light.closet": rare})
    ws = _ws(
        [
            _state("person.max", "home"),
            _state("light.closet", "on", hours_ago=0.2),
        ]
    )
    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert [a for a in actions if a.type == "while_away"] == []


async def test_away_samples_recorded_while_away() -> None:
    profiles = ProfileSet()
    ws = _ws(
        [
            _state("person.max", "not_home"),
            _state("light.kitchen", "on", hours_ago=0.2),
            _state("light.hall", "off"),
        ]
    )
    await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert profiles.entities["light.kitchen"].away_samples == 1
    assert profiles.entities["light.kitchen"].on_while_away_samples == 1
    assert profiles.entities["light.hall"].away_samples == 1
    assert profiles.entities["light.hall"].on_while_away_samples == 0


async def test_suppression_silences_detector() -> None:
    from mylo.memory.schema import NotificationSuppression

    memory = empty_memory()
    memory.notification_suppressions.append(
        NotificationSuppression(type="duration_anomaly", entity="light.kitchen")
    )
    profiles = ProfileSet(entities={"light.kitchen": _profile("light.kitchen")})
    ws = _ws([_state("light.kitchen", "on", hours_ago=4.0)])

    actions = await run_learned_checks(ws_client=ws, memory=memory, profiles=profiles)
    assert actions == []


async def test_naive_last_changed_no_crash() -> None:
    """A tz-naive last_changed (e.g. from HA after restart) must not crash
    the sweep and must be treated as UTC so long durations still fire."""
    profiles = ProfileSet(entities={"light.kitchen": _profile("light.kitchen")})
    # Naive ISO timestamp — no offset, no Z.  Use a date far in the past so
    # the duration is well beyond threshold (3600s) regardless of test-run time.
    ws = _ws(
        [
            {
                "entity_id": "light.kitchen",
                "state": "on",
                "last_changed": "2026-06-01T08:00:00",
                "attributes": {},
            }
        ]
    )
    # last_changed is days ago (treated as UTC); duration well beyond 3600s threshold.
    # We only assert no crash and at least one duration_anomaly action fires.
    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert len(actions) >= 1
    assert actions[0].type == "duration_anomaly"


async def test_all_persons_unavailable_no_while_away_and_no_samples() -> None:
    """If all person entities are unavailable/unknown we can't determine absence.
    Must NOT fire while_away and must NOT record away samples."""
    rare = _profile("light.closet", away_samples=10, on_while_away_samples=0)
    profiles = ProfileSet(entities={"light.closet": rare})
    ws = _ws(
        [
            _state("person.max", "unavailable"),
            _state("light.closet", "on", hours_ago=0.2),
        ]
    )
    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    while_away = [a for a in actions if a.type == "while_away"]
    assert while_away == []
    # No new samples — persons were all unavailable, not definitively away.
    assert profiles.entities["light.closet"].away_samples == 10


async def test_away_samples_recorded_once_per_day() -> None:
    """Two run_learned_checks calls on the same day while away record
    only one sample per entity (second call is skipped)."""
    profiles = ProfileSet()
    ws = _ws(
        [
            _state("person.max", "not_home"),
            _state("light.kitchen", "on", hours_ago=0.2),
        ]
    )
    # Both calls use real now() → same date → second call must be skipped.
    await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert profiles.entities["light.kitchen"].away_samples == 1


async def test_while_away_confidence_uses_away_samples() -> None:
    """while_away action confidence is based on away_samples, not days_observed.
    With away_samples=10 before the call, the first call records one more sample
    (11 total, since once-per-day rule allows recording on a fresh ProfileSet),
    so confidence = 11/16."""
    # Build a profile eligible for while-away: 10 away samples, rarely on while away.
    # days_observed is LOW so we can verify confidence is NOT from days_observed.
    rare = _profile("light.closet", away_samples=10, on_while_away_samples=0, days_observed=0)
    rare.cycle_count = 0  # not duration-eligible, so only while_away fires
    profiles = ProfileSet(entities={"light.closet": rare})
    ws = _ws(
        [
            _state("person.max", "not_home"),
            _state("light.closet", "on", hours_ago=0.2),
        ]
    )
    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    while_away = [a for a in actions if a.type == "while_away"]
    assert len(while_away) == 1
    # Recording happens before detection: 10 + 1 = 11 samples used for confidence.
    assert while_away[0].confidence == pytest.approx(11 / 16)


async def test_binary_sensor_motion_uses_active_verb() -> None:
    """binary_sensor with device_class 'motion' should use 'active', not 'open'."""
    p = _profile("binary_sensor.hall_motion")
    # Threshold: max(2100*1.25, 1800*2) = 3600s. State "on" for 4h = 14400s > 3600s.
    profiles = ProfileSet(entities={"binary_sensor.hall_motion": p})
    ws = _ws(
        [
            {
                "entity_id": "binary_sensor.hall_motion",
                "state": "on",
                "last_changed": (datetime.now(UTC) - timedelta(hours=4)).isoformat(),
                "attributes": {"device_class": "motion", "friendly_name": "Hall Motion"},
            }
        ]
    )
    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert len(actions) == 1
    assert "active" in actions[0].title
    assert "open" not in actions[0].title
    assert "active" in actions[0].message
