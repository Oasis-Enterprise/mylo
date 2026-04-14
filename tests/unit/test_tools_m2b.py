"""Tests for M2b tier-1 tools.

One fixture builds a shared in-memory HA context with a small but realistic
snapshot: 2 areas, 3 devices, 6 entities, a few states, a dashboard config,
an automation config, and a couple of system-log entries. Each tool test
verifies the happy path plus the one or two failure modes worth asserting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.config import AppConfig
from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute

# ─── Fake client that can answer every M2b command ───────────────────────────


class _FakeClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ not in self.responses:
            raise AssertionError(f"unexpected command: {type_}")
        value = self.responses[type_]
        if callable(value):
            return value(**kwargs)
        return value


# ─── Context fixture ─────────────────────────────────────────────────────────


@pytest.fixture
def _ctx(tmp_path: Path) -> ToolContext:
    reg = Registries()
    reg.areas = {
        "kitchen": AreaEntry.from_raw({"area_id": "kitchen", "name": "Kitchen"}),
        "garage": AreaEntry.from_raw({"area_id": "garage", "name": "Garage"}),
    }
    reg.devices = {
        "dev_thermo": DeviceEntry.from_raw(
            {
                "id": "dev_thermo",
                "name": "Thermostat",
                "manufacturer": "Ecobee",
                "model": "Smart Enhanced",
                "area_id": "kitchen",
            }
        ),
        "dev_hue": DeviceEntry.from_raw(
            {
                "id": "dev_hue",
                "name": "Hue Bulb",
                "manufacturer": "Signify",
                "model": "Hue White",
                "area_id": "kitchen",
            }
        ),
        "dev_garage_opener": DeviceEntry.from_raw(
            {
                "id": "dev_garage_opener",
                "name": "Garage Opener",
                "manufacturer": "Shelly",
                "model": "Plus 1",
                "area_id": "garage",
            }
        ),
    }
    reg.entities = {
        "climate.thermostat": EntityEntry.from_raw(
            {
                "entity_id": "climate.thermostat",
                "original_name": "Thermostat",
                "platform": "ecobee",
                "device_id": "dev_thermo",
                "area_id": None,
                "labels": [],
            }
        ),
        "light.kitchen_overhead": EntityEntry.from_raw(
            {
                "entity_id": "light.kitchen_overhead",
                "original_name": "Kitchen Overhead",
                "platform": "hue",
                "device_id": "dev_hue",
                "area_id": "kitchen",
                "labels": [],
            }
        ),
        "cover.garage_door": EntityEntry.from_raw(
            {
                "entity_id": "cover.garage_door",
                "original_name": "Garage Door",
                "platform": "shelly",
                "device_id": "dev_garage_opener",
                "area_id": "garage",
                "labels": [],
            }
        ),
        "automation.morning_routine": EntityEntry.from_raw(
            {
                "entity_id": "automation.morning_routine",
                "original_name": "Morning Routine",
                "platform": "automation",
                "labels": [],
            }
        ),
    }

    states = {
        "climate.thermostat": {
            "entity_id": "climate.thermostat",
            "state": "cool",
            "attributes": {},
        },
        "light.kitchen_overhead": {
            "entity_id": "light.kitchen_overhead",
            "state": "on",
            "attributes": {"brightness": 255, "friendly_name": "Kitchen Overhead"},
        },
        "cover.garage_door": {
            "entity_id": "cover.garage_door",
            "state": "closed",
            "attributes": {},
        },
        "automation.morning_routine": {
            "entity_id": "automation.morning_routine",
            "state": "on",
            "attributes": {
                "friendly_name": "Morning Routine",
                "last_triggered": "2026-04-14T07:00:00+00:00",
            },
        },
    }

    responses: dict[str, Any] = {
        "get_states": list(states.values()),
        "get_config": {
            "version": "2026.4.2",
            "location_name": "Home",
            "time_zone": "America/Chicago",
            "unit_system": {"temperature": "°F"},
        },
        "lovelace/dashboards/list": [
            {"url_path": "lovelace", "title": "Main", "mode": "storage"},
        ],
        "lovelace/config": {
            "title": "Main",
            "views": [
                {"path": "home", "title": "Home", "cards": [{"type": "entities"}]},
                {"path": "energy", "title": "Energy", "cards": []},
            ],
        },
        "logbook/get_events": [
            {"entity_id": "light.kitchen_overhead", "when": "t1", "message": "turned on"},
        ],
        "history/history_during_period": [
            [
                {"state": "off", "last_changed": "t0"},
                {"state": "on", "last_changed": "t1"},
            ]
        ],
        "system_log/list": [
            {
                "level": "ERROR",
                "source": "homeassistant.components.hue",
                "message": "uh oh",
                "timestamp": 1700000000,
                "count": 1,
                "exception": "Traceback...",
            },
            {
                "level": "WARNING",
                "source": "homeassistant.core",
                "message": "meh",
                "timestamp": 1700000100,
                "count": 1,
            },
        ],
        "config/automation/config": {
            "alias": "Morning Routine",
            "trigger": [{"platform": "time", "at": "07:00"}],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.kitchen_overhead"}}
            ],
        },
        # verify_change invokes registries.refresh() which reissues these four
        # list commands; supply the current in-memory state verbatim.
        "config/entity_registry/list": [
            {
                "entity_id": eid,
                "platform": e.platform,
                "device_id": e.device_id,
                "area_id": e.area_id,
                "original_name": e.original_name,
                "name": e.name,
                "labels": list(e.labels),
                "disabled_by": e.disabled_by,
                "hidden_by": e.hidden_by,
            }
            for eid, e in {}.items()  # populated below after reg built
        ],
        "config/device_registry/list": [],
        "config/area_registry/list": [],
        "config/label_registry/list": [],
    }

    # Fill in the registry list commands from the actual registry now that
    # it's been populated above.
    responses["config/entity_registry/list"] = [
        {
            "entity_id": eid,
            "platform": e.platform,
            "device_id": e.device_id,
            "area_id": e.area_id,
            "original_name": e.original_name,
            "name": e.name,
            "labels": list(e.labels),
            "disabled_by": e.disabled_by,
            "hidden_by": e.hidden_by,
        }
        for eid, e in reg.entities.items()
    ]
    responses["config/device_registry/list"] = [
        {
            "id": d.id,
            "name": d.name,
            "name_by_user": d.name_by_user,
            "manufacturer": d.manufacturer,
            "model": d.model,
            "area_id": d.area_id,
            "labels": list(d.labels),
            "disabled_by": d.disabled_by,
        }
        for d in reg.devices.values()
    ]
    responses["config/area_registry/list"] = [
        {"area_id": a.area_id, "name": a.name, "floor_id": a.floor_id, "labels": list(a.labels)}
        for a in reg.areas.values()
    ]
    responses["config/label_registry/list"] = [
        {"label_id": lbl.label_id, "name": lbl.name, "color": lbl.color}
        for lbl in reg.labels.values()
    ]

    client = _FakeClient(responses)

    config = AppConfig(
        api_key="",
        llm_provider="anthropic",
        model="x",
        reconciliation_model="y",
        sync_frequency="nightly",
        memory_token_limit=8000,
        proactive_notifications=False,
        max_daily_notifications=3,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        supervisor_token=None,
        ha_config_dir=tmp_path,
        mylo_data_dir=tmp_path / ".mylo",
    )
    return ToolContext(ws_client=client, registries=reg, config=config)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _load_tools() -> Any:
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


# ─── query_devices ──────────────────────────────────────────────────────────


async def test_query_devices_by_area(_ctx: ToolContext) -> None:
    result = await execute("query_devices", {"filter": {"area": "kitchen"}}, _ctx)
    assert result.status.value == "ok"
    ids = sorted(d["id"] for d in result.data["devices"])
    assert ids == ["dev_hue", "dev_thermo"]
    assert result.data["area"] == "Kitchen"
    assert "Ecobee" in result.data["by_manufacturer"]


async def test_query_devices_manufacturer_substring(_ctx: ToolContext) -> None:
    result = await execute("query_devices", {"filter": {"manufacturer": "signify"}}, _ctx)
    assert [d["id"] for d in result.data["devices"]] == ["dev_hue"]


async def test_query_devices_integration_filter(_ctx: ToolContext) -> None:
    result = await execute(
        "query_devices", {"filter": {"integration": "hue"}, "include_entities": True}, _ctx
    )
    assert result.data["devices_found"] == 1
    device = result.data["devices"][0]
    assert device["id"] == "dev_hue"
    assert any(e["entity_id"] == "light.kitchen_overhead" for e in device["entities"])


async def test_query_devices_unknown_area(_ctx: ToolContext) -> None:
    result = await execute("query_devices", {"filter": {"area": "attic"}}, _ctx)
    assert result.error_code == "area_not_found"


# ─── query_automations ──────────────────────────────────────────────────────


async def test_query_automations_lists_automations(_ctx: ToolContext) -> None:
    result = await execute("query_automations", {}, _ctx)
    assert result.status.value == "ok"
    assert result.data["count"] == 1
    assert result.data["items"][0]["entity_id"] == "automation.morning_routine"
    assert result.data["items"][0]["last_triggered"]


async def test_query_automations_enabled_filter(_ctx: ToolContext) -> None:
    result = await execute("query_automations", {"filter": {"enabled": False}}, _ctx)
    assert result.data["count"] == 0


async def test_query_automations_include_config(_ctx: ToolContext) -> None:
    result = await execute("query_automations", {"include_config": True}, _ctx)
    assert result.data["items"][0]["config"]["alias"] == "Morning Routine"


# ─── query_dashboard ────────────────────────────────────────────────────────


async def test_query_dashboard_list(_ctx: ToolContext) -> None:
    result = await execute("query_dashboard", {}, _ctx)
    ids = [d["dashboard_id"] for d in result.data["dashboards"]]
    assert None in ids  # default dashboard
    assert "lovelace" in ids


async def test_query_dashboard_by_id_returns_view_summaries(_ctx: ToolContext) -> None:
    result = await execute("query_dashboard", {"dashboard_id": "lovelace"}, _ctx)
    assert result.data["view_count"] == 2
    paths = [v["path"] for v in result.data["views"]]
    assert paths == ["home", "energy"]
    # Card count threaded through.
    assert next(v["card_count"] for v in result.data["views"] if v["path"] == "home") == 1


async def test_query_dashboard_view_id_returns_full_view(_ctx: ToolContext) -> None:
    result = await execute("query_dashboard", {"dashboard_id": "lovelace", "view_id": "home"}, _ctx)
    assert result.data["view"]["path"] == "home"
    assert result.data["view"]["cards"][0]["type"] == "entities"


async def test_query_dashboard_unknown_view(_ctx: ToolContext) -> None:
    result = await execute(
        "query_dashboard", {"dashboard_id": "lovelace", "view_id": "missing"}, _ctx
    )
    assert result.error_code == "view_not_found"


# ─── query_logs ─────────────────────────────────────────────────────────────


async def test_query_logs_logbook(_ctx: ToolContext) -> None:
    result = await execute(
        "query_logs", {"type": "logbook", "entity_id": "light.kitchen_overhead"}, _ctx
    )
    assert result.data["count"] == 1
    assert result.data["events"][0]["message"] == "turned on"


async def test_query_logs_state_history_requires_entity(_ctx: ToolContext) -> None:
    result = await execute("query_logs", {"type": "state_history"}, _ctx)
    assert result.error_code == "missing_param"


async def test_query_logs_system_log_filters_by_severity(_ctx: ToolContext) -> None:
    result = await execute("query_logs", {"type": "system_log", "severity": "error"}, _ctx)
    assert result.data["count"] == 1
    assert result.data["entries"][0]["level"] == "ERROR"


# ─── query_system ───────────────────────────────────────────────────────────


async def test_query_system_overview(_ctx: ToolContext) -> None:
    result = await execute("query_system", {"scope": "overview"}, _ctx)
    assert result.data["ha_version"] == "2026.4.2"
    assert result.data["entities"] == 4
    assert result.data["areas"] == 2


async def test_query_system_integrations_counts(_ctx: ToolContext) -> None:
    result = await execute("query_system", {"scope": "integrations"}, _ctx)
    domains = {i["domain"]: i["entity_count"] for i in result.data["integrations"]}
    assert domains == {"ecobee": 1, "hue": 1, "shelly": 1, "automation": 1}


async def test_query_system_addons_requires_supervisor_token(_ctx: ToolContext) -> None:
    result = await execute("query_system", {"scope": "addons"}, _ctx)
    assert result.error_code == "unavailable"


# ─── read_config_file ──────────────────────────────────────────────────────


async def test_read_config_file_returns_sanitized_content(_ctx: ToolContext) -> None:
    path = _ctx.config.ha_config_dir / "automations.yaml"
    path.write_text("- alias: Foo\n  password: !secret wifi_password\n", encoding="utf-8")

    result = await execute("read_config_file", {"path": "automations.yaml"}, _ctx)
    assert result.status.value == "ok"
    assert "[SECRET:wifi_password]" in result.data["content"]
    assert "!secret" not in result.data["content"]
    assert result.data["secrets_masked"] is True


async def test_read_config_file_rejects_path_traversal(_ctx: ToolContext) -> None:
    result = await execute("read_config_file", {"path": "../etc/passwd"}, _ctx)
    assert result.error_code in ("path_traversal", "path_outside_config")


async def test_read_config_file_rejects_secrets_yaml(_ctx: ToolContext) -> None:
    (_ctx.config.ha_config_dir / "secrets.yaml").write_text("wifi_password: hunter2")
    result = await execute("read_config_file", {"path": "secrets.yaml"}, _ctx)
    assert result.error_code == "denied_file"


async def test_read_config_file_rejects_dotstorage(_ctx: ToolContext) -> None:
    storage_dir = _ctx.config.ha_config_dir / ".storage"
    storage_dir.mkdir()
    (storage_dir / "auth").write_text("{}")
    result = await execute("read_config_file", {"path": ".storage/auth"}, _ctx)
    assert result.error_code in ("denied_directory", "unsupported_extension")


async def test_read_config_file_missing(_ctx: ToolContext) -> None:
    result = await execute("read_config_file", {"path": "nope.yaml"}, _ctx)
    assert result.error_code == "not_found"


# ─── verify_change ─────────────────────────────────────────────────────────


async def test_verify_change_entity_exists(_ctx: ToolContext) -> None:
    # verify_change calls registries.refresh() which reissues the four
    # list commands; the fixture's fake client already answers them.
    # We also need the registry's own hooks to not block — Registries has
    # an internal asyncio.Lock which is fine.
    _ctx.registries._client = _ctx.ws_client  # type: ignore[assignment]

    result = await execute(
        "verify_change",
        {
            "check_type": "entity_exists",
            "targets": ["light.kitchen_overhead", "light.missing"],
            "wait_seconds": 0,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["results"]["light.kitchen_overhead"] is True
    assert result.data["results"]["light.missing"] is False
    assert result.data["all_ok"] is False


async def test_verify_change_automation_loaded(_ctx: ToolContext) -> None:
    _ctx.registries._client = _ctx.ws_client  # type: ignore[assignment]

    result = await execute(
        "verify_change",
        {
            "check_type": "automation_loaded",
            "targets": ["automation.morning_routine"],
            "wait_seconds": 0,
        },
        _ctx,
    )
    assert result.data["results"]["automation.morning_routine"]["ok"] is True
    assert result.data["all_ok"] is True


async def test_verify_change_unimplemented(_ctx: ToolContext) -> None:
    result = await execute(
        "verify_change",
        {"check_type": "full_health", "wait_seconds": 0},
        _ctx,
    )
    assert result.error_code == "not_implemented"


# ─── memory_note ───────────────────────────────────────────────────────────


async def test_memory_note_appends_to_scratchpad(_ctx: ToolContext) -> None:
    result = await execute(
        "memory_note",
        {
            "type": "user_note",
            "scope": {"entity": "cover.garage_door"},
            "content": "Sticks sometimes. Mention before automating.",
        },
        _ctx,
    )
    assert result.status.value == "ok"
    scratchpad = _ctx.config.mylo_data_dir / "scratchpad.yaml"
    assert scratchpad.exists()
    content = scratchpad.read_text()
    assert "Sticks sometimes" in content
    assert "cover.garage_door" in content


async def test_memory_note_appends_multiple(_ctx: ToolContext) -> None:
    await execute(
        "memory_note",
        {"type": "observation", "scope": {"general": True}, "content": "One"},
        _ctx,
    )
    await execute(
        "memory_note",
        {"type": "preference", "scope": {"area": "kitchen"}, "content": "Two"},
        _ctx,
    )
    text = (_ctx.config.mylo_data_dir / "scratchpad.yaml").read_text()
    assert text.count("- {") == 2
