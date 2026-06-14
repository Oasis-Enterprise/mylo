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

"""Tests for M7b tools: modify_areas, manage_labels, modify_dashboard,
rename_entities.

All four are websocket-only (no file ops for areas/labels/dashboards) or
combine registry + file + dashboard ops (rename). A shared FakeClient
records calls and returns programmable responses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, LabelEntry, Registries
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ in self._responses:
            val = self._responses[type_]
            if callable(val):
                return val(**kwargs)
            return val
        return {}


@pytest.fixture
def _ctx(tmp_path: Path):
    reg = Registries()
    reg.areas = {
        "kitchen": AreaEntry.from_raw({"area_id": "kitchen", "name": "Kitchen"}),
    }
    reg.devices = {
        "d1": DeviceEntry.from_raw({"id": "d1", "area_id": "kitchen", "name": "Hue Bulb"}),
    }
    reg.entities = {
        "light.kitchen_overhead": EntityEntry.from_raw(
            {
                "entity_id": "light.kitchen_overhead",
                "original_name": "Kitchen Overhead",
                "device_id": "d1",
                "area_id": "kitchen",
                "labels": ["indoor"],
            }
        ),
        "sensor.kitchen_temp": EntityEntry.from_raw(
            {
                "entity_id": "sensor.kitchen_temp",
                "original_name": "Kitchen Temp",
                "labels": [],
            }
        ),
    }
    reg.labels = {
        "indoor": LabelEntry.from_raw({"label_id": "indoor", "name": "Indoor"}),
    }

    client = _FakeClient(
        responses={
            "get_states": [],
            "lovelace/dashboards/list": [],
            "lovelace/config": {"views": [{"path": "home", "title": "Home", "cards": []}]},
        }
    )
    return make_ctx(ws_client=client, registries=reg, tmp_path=tmp_path)


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


# ─── modify_areas ───────────────────────────────────────────────────────────


async def test_create_area(_ctx):
    _ctx.user_approved = True
    result = await execute(
        "modify_areas",
        {
            "action": "create",
            "new_name": "Garage",
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["action"] == "create"
    client = _ctx.ws_client
    assert any(t == "config/area_registry/create" for t, _ in client.calls)


async def test_rename_area(_ctx):
    _ctx.user_approved = True
    result = await execute(
        "modify_areas",
        {"action": "rename", "area_id": "kitchen", "new_name": "Main Kitchen"},
        _ctx,
    )
    assert result.status.value == "ok"


async def test_assign_device_to_area(_ctx):
    _ctx.user_approved = True
    result = await execute(
        "modify_areas",
        {"action": "assign_device", "area_id": "kitchen", "target_ids": ["d1"]},
        _ctx,
    )
    assert result.status.value == "ok"
    client = _ctx.ws_client
    assert any(
        t == "config/device_registry/update" and kw.get("device_id") == "d1"
        for t, kw in client.calls
    )


async def test_create_area_requires_approval(_ctx):
    _ctx.user_approved = False
    result = await execute(
        "modify_areas",
        {"action": "create", "new_name": "Garage"},
        _ctx,
    )
    assert result.error_code == "confirmation_required"


# ─── manage_labels ──────────────────────────────────────────────────────────


async def test_list_labels(_ctx):
    result = await execute("manage_labels", {"action": "list"}, _ctx)
    assert result.status.value == "ok"
    assert len(result.data["labels"]) == 1
    assert result.data["labels"][0]["name"] == "Indoor"


async def test_create_label(_ctx):
    _ctx.user_approved = True
    result = await execute(
        "manage_labels",
        {"action": "create", "label": "Outdoor", "color": "#00ff00"},
        _ctx,
    )
    assert result.status.value == "ok"
    client = _ctx.ws_client
    assert any(
        t == "config/label_registry/create" and kw.get("name") == "Outdoor"
        for t, kw in client.calls
    )


async def test_assign_label_to_entity(_ctx):
    _ctx.user_approved = True
    result = await execute(
        "manage_labels",
        {
            "action": "assign",
            "label": "indoor",
            "targets": ["light.kitchen_overhead"],
        },
        _ctx,
    )
    assert result.status.value == "ok"
    client = _ctx.ws_client
    assert any(t == "config/entity_registry/update" and "labels" in kw for t, kw in client.calls)


# ─── modify_dashboard ──────────────────────────────────────────────────────


async def test_create_view_dry_run(_ctx):
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "dashboard_id": None,
            "config": {"path": "mylo", "title": "Mylo", "cards": [{"type": "entities"}]},
            "dry_run": True,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    assert result.data["card_count"] == 1


async def test_create_view_apply(_ctx):
    _ctx.user_approved = True
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "dashboard_id": None,
            "config": {"path": "mylo", "title": "Mylo", "cards": []},
            "dry_run": False,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    client = _ctx.ws_client
    assert any(t == "lovelace/config/save" for t, _ in client.calls)


async def test_delete_view_dry_run(_ctx):
    result = await execute(
        "modify_dashboard",
        {"action": "delete", "dashboard_id": None, "view_path": "home", "dry_run": True},
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True


async def test_delete_nonexistent_view(_ctx):
    result = await execute(
        "modify_dashboard",
        {"action": "delete", "dashboard_id": None, "view_path": "nope", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "view_not_found"


# ─── modify_dashboard — sections layout ─────────────────────────────────────
#
# HA's sections layout nests cards inside `view.sections[i].cards`. The
# surgical ops must accept `section_index` so the model can target a
# nested card without re-emitting the whole view.


def _sections_view_dashboard() -> dict[str, Any]:
    """Realistic sections-layout dashboard config the fake client returns."""
    return {
        "views": [
            {
                "path": "rooms",
                "title": "Rooms",
                "type": "sections",
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Living"},
                            {"type": "entity", "entity": "light.kitchen_overhead"},
                        ],
                    },
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Outside & Garage"},
                            {
                                "type": "custom:mushroom-chips-card",
                                "chips": [
                                    {"type": "entity", "entity": "light.kitchen_overhead"},
                                ],
                            },
                        ],
                    },
                ],
            }
        ]
    }


@pytest.fixture
def _sections_ctx(tmp_path: Path):
    reg = Registries()
    reg.entities = {
        "light.kitchen_overhead": EntityEntry.from_raw(
            {"entity_id": "light.kitchen_overhead", "original_name": "Kitchen"}
        ),
    }
    client = _FakeClient(
        responses={
            "lovelace/config": _sections_view_dashboard(),
        }
    )
    return make_ctx(ws_client=client, registries=reg, tmp_path=tmp_path)


async def test_replace_card_with_section_index_targets_nested_card(_sections_ctx):
    """replace_card with section_index swaps view.sections[section].cards[index]."""
    result = await execute(
        "modify_dashboard",
        {
            "action": "replace_card",
            "dashboard_id": None,
            "view_path": "rooms",
            "section_index": 1,
            "card_index": 1,
            "config": {
                "type": "custom:mushroom-chips-card",
                "chips": [
                    {"type": "entity", "entity": "light.kitchen_overhead"},
                    {"type": "entity", "entity": "light.kitchen_overhead"},
                ],
            },
            "dry_run": True,
        },
        _sections_ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["preview"] is True
    assert result.data["section_index"] == 1
    assert result.data["card_index"] == 1
    assert result.data["old_card_type"] == "custom:mushroom-chips-card"


async def test_replace_card_without_section_index_on_sections_view_errors(_sections_ctx):
    """Targeting a sections-layout view via top-level card_index alone must
    return a helpful error pointing the model at section_index.
    """
    result = await execute(
        "modify_dashboard",
        {
            "action": "replace_card",
            "dashboard_id": None,
            "view_path": "rooms",
            "card_index": 0,
            "config": {"type": "entity", "entity": "light.kitchen_overhead"},
            "dry_run": True,
        },
        _sections_ctx,
    )
    assert result.error_code == "section_index_required"


async def test_add_cards_with_section_index_appends_to_section(_sections_ctx):
    """add_cards with section_index appends to view.sections[section].cards."""
    result = await execute(
        "modify_dashboard",
        {
            "action": "add_cards",
            "dashboard_id": None,
            "view_path": "rooms",
            "section_index": 0,
            "cards": [{"type": "entity", "entity": "light.kitchen_overhead"}],
            "dry_run": True,
        },
        _sections_ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["preview"] is True
    assert result.data["section_index"] == 0
    assert result.data["cards_added"] == 1
    # Section 0 had 2 cards; after add, 3.
    assert result.data["total_cards"] == 3


async def test_remove_card_with_section_index_drops_nested_card(_sections_ctx):
    """remove_card with section_index removes view.sections[section].cards[index]."""
    result = await execute(
        "modify_dashboard",
        {
            "action": "remove_card",
            "dashboard_id": None,
            "view_path": "rooms",
            "section_index": 1,
            "card_index": 1,
            "dry_run": True,
        },
        _sections_ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["preview"] is True
    assert result.data["section_index"] == 1
    assert result.data["removed_card_type"] == "custom:mushroom-chips-card"
    assert result.data["remaining_cards"] == 1


async def test_replace_card_with_invalid_section_index_errors(_sections_ctx):
    result = await execute(
        "modify_dashboard",
        {
            "action": "replace_card",
            "dashboard_id": None,
            "view_path": "rooms",
            "section_index": 99,
            "card_index": 0,
            "config": {"type": "entity", "entity": "light.kitchen_overhead"},
            "dry_run": True,
        },
        _sections_ctx,
    )
    assert result.error_code == "section_not_found"


# ─── rename_entities ─────────────────────────────────────────────────────────


async def test_rename_dry_run_shows_references(_ctx):
    # Write a file with the entity_id so the scanner finds it.
    agent = _ctx.config.ha_config_dir / "packages" / "agent.yaml"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text("automation:\n  - trigger:\n      entity_id: light.kitchen_overhead\n")

    result = await execute(
        "rename_entities",
        {
            "renames": [
                {
                    "entity_id": "light.kitchen_overhead",
                    "new_entity_id": "light.kitchen_main",
                }
            ],
            "update_references": True,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    refs = result.data["references_found"].get("light.kitchen_overhead", [])
    assert any(r["path"] == "packages/agent.yaml" for r in refs)


async def test_rename_rejects_nonexistent_entity(_ctx):
    result = await execute(
        "rename_entities",
        {
            "renames": [{"entity_id": "sensor.ghost", "new_entity_id": "sensor.real"}],
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "entity_not_found"
    assert "did_you_mean" in result.data


async def test_rename_rejects_existing_target(_ctx):
    result = await execute(
        "rename_entities",
        {
            "renames": [
                {
                    "entity_id": "light.kitchen_overhead",
                    "new_entity_id": "sensor.kitchen_temp",
                }
            ],
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "already_exists"


async def test_rename_apply_calls_registry_update(_ctx):
    _ctx.user_approved = True
    result = await execute(
        "rename_entities",
        {
            "renames": [
                {
                    "entity_id": "light.kitchen_overhead",
                    "new_friendly_name": "Kitchen Main Light",
                }
            ],
            "dry_run": False,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    client = _ctx.ws_client
    assert any(
        t == "config/entity_registry/update"
        and kw.get("entity_id") == "light.kitchen_overhead"
        and kw.get("name") == "Kitchen Main Light"
        for t, kw in client.calls
    )


# ─── manage_helpers — input_button + schedule ────────────────────────────────


@pytest.fixture
def _helpers_ctx(tmp_path: Path):
    reg = Registries()
    reg.entities = {}
    client = _FakeClient(responses={"config/input_button/create": {"id": "btn1"}})
    return make_ctx(ws_client=client, registries=reg, tmp_path=tmp_path)


async def test_create_input_button(_helpers_ctx):
    """create input_button routes to config/input_button/create with name."""
    _helpers_ctx.user_approved = True
    result = await execute(
        "manage_helpers",
        {"action": "create", "helper_type": "input_button", "name": "Ring Doorbell"},
        _helpers_ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["helper_type"] == "input_button"
    client = _helpers_ctx.ws_client
    assert any(
        t == "config/input_button/create" and kw.get("name") == "Ring Doorbell"
        for t, kw in client.calls
    )


async def test_create_schedule_with_blocks(_helpers_ctx):
    """create schedule routes to config/schedule/create with weekday blocks in payload."""
    _helpers_ctx.ws_client._responses["config/schedule/create"] = {"id": "sched1"}
    _helpers_ctx.user_approved = True
    blocks = {"monday": [{"from": "08:00:00", "to": "17:00:00"}]}
    result = await execute(
        "manage_helpers",
        {
            "action": "create",
            "helper_type": "schedule",
            "name": "Work Hours",
            "schedule_blocks": blocks,
        },
        _helpers_ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["helper_type"] == "schedule"
    client = _helpers_ctx.ws_client
    matching = [(t, kw) for t, kw in client.calls if t == "config/schedule/create"]
    assert matching, "expected config/schedule/create call"
    _, kw = matching[0]
    assert kw.get("monday") == [{"from": "08:00:00", "to": "17:00:00"}]


async def test_delete_schedule_routes_correctly(_helpers_ctx):
    """delete schedule routes to config/schedule/delete with schedule_id."""
    _helpers_ctx.user_approved = True
    result = await execute(
        "manage_helpers",
        {"action": "delete", "helper_type": "schedule", "helper_id": "work_hours"},
        _helpers_ctx,
    )
    assert result.status.value == "ok", result
    client = _helpers_ctx.ws_client
    assert any(
        t == "config/schedule/delete" and kw.get("schedule_id") == "work_hours"
        for t, kw in client.calls
    )


async def test_create_helper_requires_approval(_helpers_ctx):
    """create/update/delete gated when user_approved is False."""
    _helpers_ctx.user_approved = False
    result = await execute(
        "manage_helpers",
        {"action": "create", "helper_type": "input_button", "name": "Test"},
        _helpers_ctx,
    )
    assert result.error_code == "confirmation_required"
