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

"""End-to-end test of the query_entities tool against an in-memory registry
and a stub websocket client that replies to ``get_states``.
"""

from __future__ import annotations

from typing import Any

import pytest

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries
from mylo.tools import executor as tool_executor
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


def _ctx_with_entities(count: int) -> ToolContext:
    """Build a ctx with ``count`` synthetic light entities, all with a
    matching (off) state — for exercising the row-count downgrade
    thresholds in the detail parameter."""
    reg = Registries()
    reg.entities = {
        f"light.n{i}": EntityEntry.from_raw(
            {
                "entity_id": f"light.n{i}",
                "original_name": f"Light {i}",
                "platform": "hue",
                "area_id": None,
                "labels": [],
            }
        )
        for i in range(count)
    }
    states = [{"entity_id": f"light.n{i}", "state": "off"} for i in range(count)]
    client = _FakeClient(states)
    return make_ctx(ws_client=client, registries=reg, tmp_path=__import__("pathlib").Path("/tmp"))


@pytest.fixture(autouse=True)
def _load_tool() -> Any:
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    # The executor's read-result cache is keyed on (tool_name, raw_params)
    # only, module-global across tests — two tests calling query_entities
    # with identical params against different registries would otherwise
    # collide on a stale cached result.
    tool_executor.invalidate("query_entities")
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


async def test_detail_full_adds_key_attrs(_ctx: ToolContext) -> None:
    result = await execute(
        "query_entities",
        {"filter": {"domain": "light", "state": "on"}, "detail": "full"},
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
        {"filter": {"pattern": "hue_white_lamp_1"}, "detail": "standard"},
        _ctx,
    )
    e = result.data["entities"][0]
    assert e["friendly_name"] == "Living Room Lamp 1"


async def test_detail_downgrades_to_minimal_above_100_returned() -> None:
    """detail=full is forced to minimal when >100 rows would be returned
    (token-bomb guard), but stays full on a narrow match even with the
    high default limit."""
    reg = Registries()
    reg.entities = {
        f"light.l{i}": EntityEntry.from_raw(
            {"entity_id": f"light.l{i}", "original_name": f"L{i}", "platform": "hue", "labels": []}
        )
        for i in range(150)
    }
    states = [
        {"entity_id": f"light.l{i}", "state": "on", "attributes": {"brightness": 5}}
        for i in range(150)
    ]
    ctx = make_ctx(
        ws_client=_FakeClient(states),
        registries=reg,
        tmp_path=__import__("pathlib").Path("/tmp"),
    )
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    try:
        # 150 returned at full detail → downgraded to minimal (no attributes).
        big = await execute(
            "query_entities", {"filter": {"domain": "light"}, "detail": "full"}, ctx
        )
        assert big.status.value == "ok"
        assert big.data["entities_found"] == 150
        assert all("key_attributes" not in e for e in big.data["entities"])
        # Narrow match keeps full detail despite the high default limit.
        small = await execute(
            "query_entities",
            {"filter": {"pattern": "light.l1$"}, "detail": "full"},
            ctx,
        )
        assert small.data["entities_found"] == 1
        assert "key_attributes" in small.data["entities"][0]
    finally:
        tool_registry._reset_for_tests()


async def test_ids_detail_returns_id_name_and_domain() -> None:
    ctx = _ctx_with_entities(count=3)
    result = await execute("query_entities", {"detail": "ids", "limit": 10}, ctx)
    rows = result.data["entities"]
    assert len(rows) == 3
    assert set(rows[0].keys()) == {"entity_id", "friendly_name", "domain"}


async def test_large_gather_downgrades_to_ids() -> None:
    ctx = _ctx_with_entities(count=501)
    result = await execute("query_entities", {"detail": "full", "limit": 2000}, ctx)
    rows = result.data["entities"]
    assert len(rows) == 501
    assert set(rows[0].keys()) == {"entity_id", "friendly_name", "domain"}
    # Regression: shape_entity_ids must carry "domain" so summarize_entities
    # can group rows correctly instead of bucketing everything under "?".
    assert "?" not in result.data["summary"]


async def test_medium_gather_downgrades_to_minimal() -> None:
    ctx = _ctx_with_entities(count=101)
    result = await execute("query_entities", {"detail": "full", "limit": 2000}, ctx)
    row = result.data["entities"][0]
    # "key_attributes" only appears when detail=full is actually honored
    # (shape_entity with include_attributes=True); "area" is present on
    # both minimal and full rows, so the absence of key_attributes is what
    # proves the downgrade to minimal actually happened.
    assert "area" in row
    assert "key_attributes" not in row


async def test_ids_detail_falls_back_to_entity_id_like_minimal() -> None:
    """shape_entity_ids must derive friendly_name with the same priority
    order as shape_entity_minimal: when the registry has no name/
    original_name and the state carries no friendly_name attribute, both
    fall back to the bare entity_id — never None."""
    reg = Registries()
    reg.entities = {
        "light.mystery": EntityEntry.from_raw(
            {
                "entity_id": "light.mystery",
                "name": None,
                "original_name": None,
                "platform": "hue",
                "area_id": None,
                "labels": [],
            }
        ),
    }
    states = [{"entity_id": "light.mystery", "state": "off", "attributes": {}}]
    ctx = make_ctx(
        ws_client=_FakeClient(states),
        registries=reg,
        tmp_path=__import__("pathlib").Path("/tmp"),
    )
    result = await execute("query_entities", {"detail": "ids", "limit": 10}, ctx)
    row = result.data["entities"][0]
    assert row["friendly_name"] == "light.mystery"
