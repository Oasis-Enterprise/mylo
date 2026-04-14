"""End-to-end test of the query_entities tool against an in-memory registry
and a stub websocket client that replies to ``get_states``.
"""

from __future__ import annotations

from typing import Any

import pytest

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _FakeClient:
    """Minimal stand-in for HaWsClient — only needs send_command('get_states')."""

    def __init__(self, states: list[dict[str, Any]]) -> None:
        self._states = states

    async def send_command(self, type_: str, **_: Any) -> Any:
        if type_ == "get_states":
            return self._states
        raise AssertionError(f"unexpected command {type_!r}")


@pytest.fixture
def _ctx() -> ToolContext:
    reg = Registries()
    reg.areas = {
        "kitchen": AreaEntry.from_raw({"area_id": "kitchen", "name": "Kitchen"}),
        "garage": AreaEntry.from_raw({"area_id": "garage", "name": "Garage"}),
    }
    reg.devices = {
        "d1": DeviceEntry.from_raw({"id": "d1", "area_id": "kitchen"}),
    }
    reg.entities = {
        "light.kitchen_overhead": EntityEntry.from_raw(
            {
                "entity_id": "light.kitchen_overhead",
                "original_name": "Kitchen Overhead",
                "platform": "hue",
                "device_id": "d1",
                "area_id": None,
                "labels": [],
            }
        ),
        "light.kitchen_pendant": EntityEntry.from_raw(
            {
                "entity_id": "light.kitchen_pendant",
                "original_name": "Kitchen Pendant",
                "platform": "hue",
                "device_id": "d1",
                "area_id": "kitchen",
                "labels": [],
            }
        ),
        "sensor.garage_temp": EntityEntry.from_raw(
            {
                "entity_id": "sensor.garage_temp",
                "original_name": "Garage Temperature",
                "platform": "esphome",
                "area_id": "garage",
                "labels": [],
            }
        ),
    }
    states = [
        {
            "entity_id": "light.kitchen_overhead",
            "state": "on",
            "attributes": {"brightness": 128},
        },
        {
            "entity_id": "light.kitchen_pendant",
            "state": "off",
            "attributes": {},
        },
        {
            "entity_id": "sensor.garage_temp",
            "state": "18.2",
            "attributes": {"device_class": "temperature"},
        },
    ]
    client = _FakeClient(states)

    return make_ctx(ws_client=client, registries=reg, tmp_path=__import__("pathlib").Path("/tmp"))


@pytest.fixture(autouse=True)
def _load_tool() -> Any:
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


async def test_filter_by_area_name_case_insensitive(_ctx: ToolContext) -> None:
    result = await execute("query_entities", {"filter": {"area": "kitchen"}}, _ctx)
    assert result.status.value == "ok"
    ids = sorted(e["entity_id"] for e in result.data["entities"])
    assert ids == ["light.kitchen_overhead", "light.kitchen_pendant"]
    assert result.data["area"] == "Kitchen"
    assert "2 lights (1 on)" in result.data["summary"]


async def test_unknown_area_returns_did_you_mean(_ctx: ToolContext) -> None:
    result = await execute("query_entities", {"filter": {"area": "batcave"}}, _ctx)
    assert result.error_code == "area_not_found"
    assert "Garage" in result.data["did_you_mean"]
    assert "Kitchen" in result.data["did_you_mean"]


async def test_filter_by_domain(_ctx: ToolContext) -> None:
    result = await execute("query_entities", {"filter": {"domain": "sensor"}}, _ctx)
    assert result.status.value == "ok"
    assert result.data["entities_found"] == 1
    assert result.data["entities"][0]["entity_id"] == "sensor.garage_temp"


async def test_pattern_regex_matches_both_id_and_name(_ctx: ToolContext) -> None:
    result = await execute("query_entities", {"filter": {"pattern": "pendant"}}, _ctx)
    assert result.data["entities_found"] == 1
    assert result.data["entities"][0]["entity_id"] == "light.kitchen_pendant"


async def test_filter_by_state(_ctx: ToolContext) -> None:
    result = await execute("query_entities", {"filter": {"state": "on"}}, _ctx)
    assert result.data["entities_found"] == 1
    assert result.data["entities"][0]["entity_id"] == "light.kitchen_overhead"


async def test_include_attributes_adds_key_attrs(_ctx: ToolContext) -> None:
    result = await execute(
        "query_entities",
        {"filter": {"domain": "light", "state": "on"}, "include_attributes": True},
        _ctx,
    )
    e = result.data["entities"][0]
    assert e["key_attributes"] == {"brightness": "50%"}


async def test_filter_by_device_class(_ctx: ToolContext) -> None:
    result = await execute("query_entities", {"filter": {"device_class": "temperature"}}, _ctx)
    assert result.data["entities_found"] == 1
    assert result.data["entities"][0]["entity_id"] == "sensor.garage_temp"


async def test_invalid_regex_error(_ctx: ToolContext) -> None:
    result = await execute("query_entities", {"filter": {"pattern": "[unclosed"}}, _ctx)
    assert result.error_code == "invalid_regex"


async def test_limit_truncates_and_flags(_ctx: ToolContext) -> None:
    result = await execute("query_entities", {"limit": 1}, _ctx)
    assert result.data["entities_found"] == 1
    assert result.data["truncated"] is True
    assert result.data["total_before_limit"] == 3


async def test_disabled_entities_excluded_by_default(_ctx: ToolContext) -> None:
    # Mark one entity as disabled_by integration.
    from mylo.ha.registries import EntityEntry

    disabled = EntityEntry.from_raw(
        {
            "entity_id": "sensor.diagnostic_rssi",
            "original_name": "RSSI",
            "platform": "shelly",
            "labels": [],
            "disabled_by": "integration",
        }
    )
    _ctx.registries.entities[disabled.entity_id] = disabled

    # Default: disabled excluded.
    result = await execute("query_entities", {"filter": {"domain": "sensor"}}, _ctx)
    ids = {e["entity_id"] for e in result.data["entities"]}
    assert "sensor.diagnostic_rssi" not in ids

    # Opt-in: included.
    result = await execute(
        "query_entities",
        {"filter": {"domain": "sensor"}, "include_disabled": True},
        _ctx,
    )
    ids = {e["entity_id"] for e in result.data["entities"]}
    assert "sensor.diagnostic_rssi" in ids


async def test_friendly_name_prefers_state_attribute_over_registry_fallback(
    _ctx: ToolContext,
) -> None:
    # Add an entity where the registry has no name/original_name but the state
    # has a useful friendly_name in attributes.
    from mylo.ha.registries import EntityEntry

    _ctx.registries.entities["light.hue_white_lamp_1"] = EntityEntry.from_raw(
        {
            "entity_id": "light.hue_white_lamp_1",
            "name": None,
            "original_name": None,
            "platform": "hue",
            "labels": [],
        }
    )
    # Patch the fake client to return a friendly_name in attributes.
    _ctx.ws_client._states.append(  # type: ignore[attr-defined]
        {
            "entity_id": "light.hue_white_lamp_1",
            "state": "on",
            "attributes": {"friendly_name": "Living Room Lamp 1", "brightness": 255},
        }
    )

    result = await execute(
        "query_entities",
        {"filter": {"pattern": "hue_white_lamp_1"}},
        _ctx,
    )
    e = result.data["entities"][0]
    assert e["friendly_name"] == "Living Room Lamp 1"
