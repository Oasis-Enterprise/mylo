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
