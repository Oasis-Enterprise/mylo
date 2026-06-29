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

"""Tests for M9/M11: scheduler, notifier, hourly sweep, baselines, anomaly.

All tests use mocks/fakes — no real HA or APScheduler clock advancing.
The scheduler tests verify job registration; the notifier tests verify
quiet-hours, daily-cap, and critical-bypass logic; hourly/anomaly tests
verify detection logic against synthetic state dicts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mylo.config import AppConfig
from mylo.memory.schema import Baselines, EntityBaseline, NotificationSuppression, empty_memory
from mylo.monitor.anomaly import check_anomalies
from mylo.monitor.anomaly import reset_state as reset_anomaly_state
from mylo.monitor.baselines import _extract_mean_values
from mylo.monitor.hourly import reset_state, run_hourly_check
from mylo.monitor.notifier import Notifier, _in_quiet_hours


@pytest.fixture(autouse=True)
def _reset_monitor_state() -> None:
    reset_state()
    reset_anomaly_state()


def _make_config(**overrides: Any) -> AppConfig:
    defaults = dict(
        api_key="",
        llm_provider="anthropic",
        model="x",
        reconciliation_model="y",
        ollama_url="",
        sync_frequency="nightly",
        memory_token_limit=8000,
        proactive_notifications=True,
        max_daily_notifications=3,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        session_budget_usd=0.50,
        monthly_budget_usd=15.00,
        context_budget_factor=0.6,
        context_output_reserve_tokens=8000,
        working_set_max_entities=40,
        notification_method="persistent",
        supervisor_token=None,
        ha_config_dir="/tmp",
        mylo_data_dir="/tmp/.mylo",
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


# ─── Notifier: quiet hours ──────────────────────────────────────────────────


def test_quiet_hours_overnight_in_range() -> None:
    # 23:00 is inside 22:00→07:00.
    assert _in_quiet_hours(datetime(2026, 4, 15, 23, 0, tzinfo=UTC), "22:00", "07:00")


def test_quiet_hours_overnight_before_range() -> None:
    # 21:00 is before 22:00→07:00.
    assert not _in_quiet_hours(datetime(2026, 4, 15, 21, 0, tzinfo=UTC), "22:00", "07:00")


def test_quiet_hours_overnight_after_range() -> None:
    # 08:00 is after 22:00→07:00.
    assert not _in_quiet_hours(datetime(2026, 4, 15, 8, 0, tzinfo=UTC), "22:00", "07:00")


def test_quiet_hours_same_day_range() -> None:
    # 14:00 is inside 13:00→15:00.
    assert _in_quiet_hours(datetime(2026, 4, 15, 14, 0, tzinfo=UTC), "13:00", "15:00")


# ─── Notifier: daily cap + critical bypass ──────────────────────────────────


async def test_notifier_respects_daily_cap() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=None)
    # Disable quiet hours (same start/end = zero window) so the cap
    # test doesn't depend on what hour the CI runs at.
    config = _make_config(
        max_daily_notifications=2,
        quiet_hours_start="00:00",
        quiet_hours_end="00:00",
        notification_method="persistent",
    )
    notifier = Notifier(ws_client=ws, config=config)

    await notifier.send(title="a", message="a", notification_id="a", severity="critical")
    await notifier.send(title="b", message="b", notification_id="b", severity="critical")
    # First two are critical to bypass quiet hours entirely; cap still applies to normal.
    capped = await notifier.send(title="c", message="c", notification_id="c")
    assert capped is False
    assert ws.send_command.call_count == 2


async def test_notifier_critical_bypasses_cap_and_quiet() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=None)
    config = _make_config(
        max_daily_notifications=0,
        quiet_hours_start="00:00",
        quiet_hours_end="23:59",
        notification_method="persistent",
    )
    notifier = Notifier(ws_client=ws, config=config)
    result = await notifier.send(
        title="fire", message="fire", notification_id="fire", severity="critical"
    )
    assert result is True
    assert ws.send_command.call_count == 1


async def test_notifier_suppressed_by_memory_filter() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=None)
    config = _make_config(quiet_hours_start="00:00", quiet_hours_end="00:00")
    mem = empty_memory()
    mem.notification_suppressions.append(NotificationSuppression(type="stale_automation"))
    notifier = Notifier(ws_client=ws, config=config, memory=mem)

    result = await notifier.send(
        title="stale",
        message="stale",
        notification_id="x",
        notification_type="stale_automation",
    )
    assert result is False
    assert ws.send_command.call_count == 0


async def test_notifier_suppression_entity_scoped() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=None)
    config = _make_config(quiet_hours_start="00:00", quiet_hours_end="00:00")
    mem = empty_memory()
    mem.notification_suppressions.append(
        NotificationSuppression(type="unavailable", entity="sensor.sprinkler")
    )
    notifier = Notifier(ws_client=ws, config=config, memory=mem)

    # Suppressed: matches entity.
    r1 = await notifier.send(
        title="a",
        message="a",
        notification_id="a",
        notification_type="unavailable",
        entity_id="sensor.sprinkler",
    )
    assert r1 is False

    # Not suppressed: different entity.
    r2 = await notifier.send(
        title="b",
        message="b",
        notification_id="b",
        notification_type="unavailable",
        entity_id="sensor.other",
    )
    assert r2 is True


async def test_notifier_wildcard_suppresses_all() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=None)
    config = _make_config(quiet_hours_start="00:00", quiet_hours_end="00:00")
    mem = empty_memory()
    mem.notification_suppressions.append(NotificationSuppression(type="*"))
    notifier = Notifier(ws_client=ws, config=config, memory=mem)

    result = await notifier.send(
        title="any",
        message="any",
        notification_id="any",
        notification_type="anomaly",
    )
    assert result is False


async def test_notifier_suppressed_when_disabled() -> None:
    ws = AsyncMock()
    config = _make_config(proactive_notifications=False)
    notifier = Notifier(ws_client=ws, config=config)
    result = await notifier.send(title="x", message="x", notification_id="x")
    assert result is False


# ─── Hourly: unavailable detection ─────────────────────────────────────────


async def test_hourly_detects_newly_unavailable() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=[
            {"entity_id": "sensor.temp", "state": "unavailable", "attributes": {}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        ]
    )

    findings = await run_hourly_check(ws_client=ws, registries=None)
    assert len(findings) == 1
    assert "sensor.temp" in findings[0]["message"]


async def test_hourly_does_not_re_report_known_unavailable() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=[
            {"entity_id": "sensor.temp", "state": "unavailable", "attributes": {}},
        ]
    )

    await run_hourly_check(ws_client=ws, registries=None)
    findings = await run_hourly_check(ws_client=ws, registries=None)
    assert len(findings) == 0  # second sweep: already known


async def test_hourly_detects_stale_automation() -> None:
    ws = AsyncMock()
    old_date = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    ws.send_command = AsyncMock(
        return_value=[
            {
                "entity_id": "automation.morning",
                "state": "on",
                "attributes": {
                    "friendly_name": "Morning routine",
                    "last_triggered": old_date,
                },
            },
        ]
    )

    findings = await run_hourly_check(ws_client=ws, registries=None)
    stale = [f for f in findings if "hasn't fired" in f["title"]]
    assert len(stale) == 1


# ─── Baselines: stats extraction ───────────────────────────────────────────


def test_extract_mean_values_filters_nulls() -> None:
    points = [
        {"mean": 22.5},
        {"mean": None},
        {"mean": 23.1},
        {"mean": "bad"},
        {"mean": 21.8},
    ]
    values = _extract_mean_values(points)
    assert len(values) == 3
    assert values == [22.5, 23.1, 21.8]


# ─── Anomaly: z-score detection ─────────────────────────────────────────────


async def test_anomaly_detects_persistent_spike() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=[
            {
                "entity_id": "sensor.power",
                "state": "450.0",
                "attributes": {
                    "friendly_name": "Power usage",
                    "unit_of_measurement": "W",
                },
            },
        ]
    )
    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
        ]
    )

    # First check: anomalous but not persistent yet — no finding.
    first = await check_anomalies(ws_client=ws, baselines=baselines)
    assert first == []

    # Second consecutive check: now it fires.
    second = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(second) == 1
    assert second[0]["z_score"] == 5.0
    assert second[0]["severity"] == "high"


async def test_anomaly_blip_resets_streak() -> None:
    ws = AsyncMock()
    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
        ]
    )
    spike = [{"entity_id": "sensor.power", "state": "450.0", "attributes": {}}]
    normal = [{"entity_id": "sensor.power", "state": "205.0", "attributes": {}}]

    ws.send_command = AsyncMock(return_value=spike)
    assert await check_anomalies(ws_client=ws, baselines=baselines) == []
    ws.send_command = AsyncMock(return_value=normal)
    assert await check_anomalies(ws_client=ws, baselines=baselines) == []
    ws.send_command = AsyncMock(return_value=spike)
    # Streak was reset — still only the first anomalous check.
    assert await check_anomalies(ws_client=ws, baselines=baselines) == []


async def test_anomaly_no_finding_within_threshold() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=[
            {"entity_id": "sensor.power", "state": "210.0", "attributes": {}},
        ]
    )

    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
        ]
    )

    findings = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(findings) == 0


async def test_anomaly_ignores_tiny_battery_fluctuation() -> None:
    """A battery at 96% with baseline 96.6+/-0.2 is technically below 3.5 sd
    (after stddev clamping) AND the absolute change (0.6%) is below
    MIN_ABSOLUTE_CHANGE. Should NOT fire."""
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=[
            {
                "entity_id": "sensor.motion_battery",
                "state": "96.0",
                "attributes": {"unit_of_measurement": "%"},
            },
        ]
    )

    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.motion_battery", metric="mean", avg=96.6, stddev=0.2),
        ]
    )

    findings = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(findings) == 0


async def test_anomaly_fires_on_real_battery_drop() -> None:
    """Vacuum at 62% with baseline 96.2+/-9.5 is a real drop (~3.6 sd, 34 units).
    Must fire after 2 consecutive checks."""
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=[
            {
                "entity_id": "sensor.vacuum_battery",
                "state": "62.0",
                "attributes": {"unit_of_measurement": "%"},
            },
        ]
    )

    baselines = Baselines(
        entities=[
            # stddev=9.5 → z = (62-96.2)/9.5 ≈ -3.60 (> 3.5 sd threshold)
            EntityBaseline(entity="sensor.vacuum_battery", metric="mean", avg=96.2, stddev=9.5),
        ]
    )

    # First check builds streak but does not fire.
    first = await check_anomalies(ws_client=ws, baselines=baselines)
    assert first == []

    # Second consecutive check fires.
    second = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(second) == 1
    assert second[0]["entity_id"] == "sensor.vacuum_battery"


async def test_anomaly_skips_non_numeric_state() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=[
            {"entity_id": "sensor.power", "state": "unavailable", "attributes": {}},
        ]
    )

    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
        ]
    )

    findings = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(findings) == 0


async def test_anomaly_gap_resets_streak() -> None:
    """An unavailable check between two spikes resets the streak.

    Spike → unavailable → spike → no fire (streak=1)
    Spike → fire (streak=2).
    """
    # avg=100, stddev=20, threshold=3 → spike value=200 gives z=5.
    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.gapped", metric="mean", avg=100.0, stddev=20.0),
        ]
    )
    spike_states = [{"entity_id": "sensor.gapped", "state": "200.0", "attributes": {}}]
    # "unavailable" means the entity is missing from the states dict entirely.
    unavail_states: list[dict[str, Any]] = []

    ws = AsyncMock()

    # Call 1: spike — streak becomes 1, no fire.
    ws.send_command = AsyncMock(return_value=spike_states)
    assert await check_anomalies(ws_client=ws, baselines=baselines, threshold=3.0) == []

    # Call 2: entity absent from states — streak reset to 0.
    ws.send_command = AsyncMock(return_value=unavail_states)
    assert await check_anomalies(ws_client=ws, baselines=baselines, threshold=3.0) == []

    # Call 3: spike again — streak restarts at 1, still no fire.
    ws.send_command = AsyncMock(return_value=spike_states)
    assert await check_anomalies(ws_client=ws, baselines=baselines, threshold=3.0) == []

    # Call 4: spike — streak=2, fires now.
    ws.send_command = AsyncMock(return_value=spike_states)
    result = await check_anomalies(ws_client=ws, baselines=baselines, threshold=3.0)
    assert len(result) == 1
    assert result[0]["entity_id"] == "sensor.gapped"


async def test_anomaly_streak_pruned_when_baseline_removed() -> None:
    """When an entity disappears from baselines, its stale streak is pruned.

    After pruning, the restored entity must accumulate a fresh streak
    before firing again.
    """
    baselines_with = Baselines(
        entities=[
            EntityBaseline(entity="sensor.pruned", metric="mean", avg=100.0, stddev=20.0),
        ]
    )
    # A different entity — ensures the loop body runs so pruning code executes,
    # but sensor.pruned is absent from `current` so its streak gets pruned.
    baselines_other = Baselines(
        entities=[
            EntityBaseline(entity="sensor.other_entity", metric="mean", avg=50.0, stddev=5.0),
        ]
    )
    spike_states = [{"entity_id": "sensor.pruned", "state": "200.0", "attributes": {}}]
    other_states = [{"entity_id": "sensor.other_entity", "state": "51.0", "attributes": {}}]

    ws = AsyncMock()

    # Call 1: spike with original baseline — streak=1, no fire.
    ws.send_command = AsyncMock(return_value=spike_states)
    assert await check_anomalies(ws_client=ws, baselines=baselines_with, threshold=3.0) == []

    # Call 2: different baselines set — sensor.pruned absent from current, streak pruned.
    ws.send_command = AsyncMock(return_value=other_states)
    await check_anomalies(ws_client=ws, baselines=baselines_other, threshold=3.0)

    # Call 3: restore original baseline + spike — streak restarted at 1, no fire.
    ws.send_command = AsyncMock(return_value=spike_states)
    assert await check_anomalies(ws_client=ws, baselines=baselines_with, threshold=3.0) == []

    # Call 4: spike again — streak=2, fires.
    ws.send_command = AsyncMock(return_value=spike_states)
    result = await check_anomalies(ws_client=ws, baselines=baselines_with, threshold=3.0)
    assert len(result) == 1
    assert result[0]["entity_id"] == "sensor.pruned"


async def test_anomaly_severity_bands() -> None:
    """Verify the severity thresholds: low=3.5-4.0, normal=4.0-4.5, high>=4.5.

    avg=200, stddev=50:
      value=385 → z=3.7 → severity "low"
      value=410 → z=4.2 → severity "normal"

    Each needs 2 consecutive checks to fire (persistence=2).
    """
    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.sev", metric="mean", avg=200.0, stddev=50.0),
        ]
    )
    ws = AsyncMock()

    # --- low severity (z=3.7) ---
    low_states = [{"entity_id": "sensor.sev", "state": "385.0", "attributes": {}}]
    ws.send_command = AsyncMock(return_value=low_states)
    assert await check_anomalies(ws_client=ws, baselines=baselines) == []
    ws.send_command = AsyncMock(return_value=low_states)
    low_result = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(low_result) == 1
    assert low_result[0]["severity"] == "low"

    # Reset between sub-cases so they're independent.
    reset_anomaly_state()

    # --- normal severity (z=4.2) ---
    normal_states = [{"entity_id": "sensor.sev", "state": "410.0", "attributes": {}}]
    ws.send_command = AsyncMock(return_value=normal_states)
    assert await check_anomalies(ws_client=ws, baselines=baselines) == []
    ws.send_command = AsyncMock(return_value=normal_states)
    normal_result = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(normal_result) == 1
    assert normal_result[0]["severity"] == "normal"
