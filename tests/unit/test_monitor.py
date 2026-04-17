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

from mylo.config import AppConfig
from mylo.memory.schema import Baselines, EntityBaseline
from mylo.monitor.anomaly import check_anomalies
from mylo.monitor.baselines import _extract_mean_values
from mylo.monitor.hourly import reset_state, run_hourly_check
from mylo.monitor.notifier import Notifier, _in_quiet_hours


def _make_config(**overrides: Any) -> AppConfig:
    defaults = dict(
        api_key="",
        llm_provider="anthropic",
        model="x",
        reconciliation_model="y",
        sync_frequency="nightly",
        memory_token_limit=8000,
        proactive_notifications=True,
        max_daily_notifications=3,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        supervisor_token=None,
        ha_config_dir="/tmp",
        mylo_data_dir="/tmp/.mylo",
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


# ─── Notifier: quiet hours ──────────────────────────────────────────────────


def test_quiet_hours_overnight_in_range() -> None:
    # 23:00 is inside 22:00→07:00.
    assert _in_quiet_hours(
        datetime(2026, 4, 15, 23, 0, tzinfo=UTC), "22:00", "07:00"
    )


def test_quiet_hours_overnight_before_range() -> None:
    # 21:00 is before 22:00→07:00.
    assert not _in_quiet_hours(
        datetime(2026, 4, 15, 21, 0, tzinfo=UTC), "22:00", "07:00"
    )


def test_quiet_hours_overnight_after_range() -> None:
    # 08:00 is after 22:00→07:00.
    assert not _in_quiet_hours(
        datetime(2026, 4, 15, 8, 0, tzinfo=UTC), "22:00", "07:00"
    )


def test_quiet_hours_same_day_range() -> None:
    # 14:00 is inside 13:00→15:00.
    assert _in_quiet_hours(
        datetime(2026, 4, 15, 14, 0, tzinfo=UTC), "13:00", "15:00"
    )


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
    )
    notifier = Notifier(ws_client=ws, config=config)
    result = await notifier.send(
        title="fire", message="fire", notification_id="fire", severity="critical"
    )
    assert result is True
    assert ws.send_command.call_count == 1


async def test_notifier_suppressed_when_disabled() -> None:
    ws = AsyncMock()
    config = _make_config(proactive_notifications=False)
    notifier = Notifier(ws_client=ws, config=config)
    result = await notifier.send(title="x", message="x", notification_id="x")
    assert result is False


# ─── Hourly: unavailable detection ─────────────────────────────────────────


async def test_hourly_detects_newly_unavailable() -> None:
    reset_state()
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=[
        {"entity_id": "sensor.temp", "state": "unavailable", "attributes": {}},
        {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
    ])

    findings = await run_hourly_check(ws_client=ws, registries=None)
    assert len(findings) == 1
    assert "sensor.temp" in findings[0]["message"]


async def test_hourly_does_not_re_report_known_unavailable() -> None:
    reset_state()
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=[
        {"entity_id": "sensor.temp", "state": "unavailable", "attributes": {}},
    ])

    await run_hourly_check(ws_client=ws, registries=None)
    findings = await run_hourly_check(ws_client=ws, registries=None)
    assert len(findings) == 0  # second sweep: already known


async def test_hourly_detects_stale_automation() -> None:
    reset_state()
    ws = AsyncMock()
    old_date = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    ws.send_command = AsyncMock(return_value=[
        {
            "entity_id": "automation.morning",
            "state": "on",
            "attributes": {
                "friendly_name": "Morning routine",
                "last_triggered": old_date,
            },
        },
    ])

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


async def test_anomaly_detects_spike() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=[
        {
            "entity_id": "sensor.power",
            "state": "450.0",
            "attributes": {
                "friendly_name": "Power usage",
                "unit_of_measurement": "W",
            },
        },
    ])

    baselines = Baselines(entities=[
        EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
    ])

    findings = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(findings) == 1
    assert findings[0]["z_score"] == 5.0
    assert "above" in findings[0]["title"]
    assert findings[0]["severity"] == "high"


async def test_anomaly_no_finding_within_threshold() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=[
        {"entity_id": "sensor.power", "state": "210.0", "attributes": {}},
    ])

    baselines = Baselines(entities=[
        EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
    ])

    findings = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(findings) == 0


async def test_anomaly_skips_non_numeric_state() -> None:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=[
        {"entity_id": "sensor.power", "state": "unavailable", "attributes": {}},
    ])

    baselines = Baselines(entities=[
        EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
    ])

    findings = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(findings) == 0
