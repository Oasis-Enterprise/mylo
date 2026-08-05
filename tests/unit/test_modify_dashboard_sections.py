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

"""Sections-first view generation for modify_dashboard.

New views default to HA's sections layout (native heading cards stay
attached to their section — the fix for titles drifting away from
their cards in masonry). Covers: auto-wrap of flat card lists, the
explicit masonry escape hatch, the add_section action, and entity-ref
validation inside nested sections (previously skipped entirely).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.registries import EntityEntry, Registries
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute


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

    def saved_config(self) -> dict[str, Any] | None:
        for type_, kwargs in self.calls:
            if type_ == "lovelace/config/save":
                return kwargs.get("config")
        return None


def _registries() -> Registries:
    reg = Registries()
    reg.entities = {
        "light.kitchen_overhead": EntityEntry.from_raw(
            {"entity_id": "light.kitchen_overhead", "original_name": "Kitchen"}
        ),
        "sensor.kitchen_temp": EntityEntry.from_raw(
            {"entity_id": "sensor.kitchen_temp", "original_name": "Kitchen Temp"}
        ),
    }
    return reg


def _sections_view_dashboard() -> dict[str, Any]:
    return {
        "views": [
            {
                "path": "rooms",
                "title": "Rooms",
                "type": "sections",
                "max_columns": 4,
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Kitchen"},
                            {"type": "tile", "entity": "light.kitchen_overhead"},
                        ],
                    },
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Climate"},
                            {"type": "tile", "entity": "sensor.kitchen_temp"},
                        ],
                    },
                ],
            }
        ]
    }


def _masonry_view_dashboard() -> dict[str, Any]:
    return {
        "views": [
            {
                "path": "legacy",
                "title": "Legacy",
                "cards": [{"type": "entities", "entities": ["light.kitchen_overhead"]}],
            }
        ]
    }


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


def _make_ctx(tmp_path: Path, dashboard: dict[str, Any]):
    from tests.unit._helpers import make_ctx

    client = _FakeClient(responses={"lovelace/config": dashboard})
    ctx = make_ctx(ws_client=client, registries=_registries(), tmp_path=tmp_path)
    ctx.user_approved = True
    return ctx


# ─── create: sections by default ────────────────────────────────────────────


async def test_create_with_flat_cards_autowraps_into_sections(tmp_path):
    """A flat card list on create becomes a single-grid sections view."""
    ctx = _make_ctx(tmp_path, {"views": []})
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Kitchen",
            "path": "kitchen",
            "cards": [{"type": "tile", "entity": "light.kitchen_overhead"}],
            "dry_run": False,
        },
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["layout"] == "sections"
    assert result.data["section_count"] == 1
    assert result.data["card_count"] == 1

    saved = ctx.ws_client.saved_config()
    view = saved["views"][0]
    assert view["type"] == "sections"
    assert view["sections"] == [
        {"type": "grid", "cards": [{"type": "tile", "entity": "light.kitchen_overhead"}]}
    ]
    assert "cards" not in view
    assert view["max_columns"] == 4


async def test_create_masonry_escape_hatch(tmp_path):
    """layout='masonry' keeps the legacy flat-cards shape."""
    ctx = _make_ctx(tmp_path, {"views": []})
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Old School",
            "path": "old",
            "layout": "masonry",
            "cards": [{"type": "tile", "entity": "light.kitchen_overhead"}],
            "dry_run": False,
        },
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["layout"] == "masonry"

    view = ctx.ws_client.saved_config()["views"][0]
    assert "sections" not in view
    assert view["cards"] == [{"type": "tile", "entity": "light.kitchen_overhead"}]


async def test_create_with_sections_config_is_not_corrupted(tmp_path):
    """A sections-shaped config must not get an empty cards list injected."""
    ctx = _make_ctx(tmp_path, {"views": []})
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "config": {
                "title": "Rooms",
                "path": "rooms",
                "type": "sections",
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Kitchen"},
                            {"type": "tile", "entity": "light.kitchen_overhead"},
                        ],
                    },
                    {
                        "type": "grid",
                        "cards": [{"type": "tile", "entity": "sensor.kitchen_temp"}],
                    },
                ],
            },
            "dry_run": False,
        },
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["layout"] == "sections"
    assert result.data["section_count"] == 2
    assert result.data["card_count"] == 3

    view = ctx.ws_client.saved_config()["views"][0]
    assert "cards" not in view
    assert view["type"] == "sections"


async def test_create_with_sections_shorthand_param(tmp_path):
    """The top-level sections param mirrors the cards shorthand."""
    ctx = _make_ctx(tmp_path, {"views": []})
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Rooms",
            "path": "rooms",
            "sections": [
                {"type": "grid", "cards": [{"type": "tile", "entity": "sensor.kitchen_temp"}]}
            ],
            "dry_run": False,
        },
        ctx,
    )
    assert result.status.value == "ok", result
    view = ctx.ws_client.saved_config()["views"][0]
    assert view["type"] == "sections"
    assert len(view["sections"]) == 1
    assert "cards" not in view


async def test_create_validates_refs_inside_sections(tmp_path):
    """Entity refs nested in sections are validated (previously skipped)."""
    ctx = _make_ctx(tmp_path, {"views": []})
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Rooms",
            "path": "rooms",
            "sections": [
                {"type": "grid", "cards": [{"type": "tile", "entity": "light.does_not_exist"}]}
            ],
            "dry_run": True,
        },
        ctx,
    )
    assert result.error_code == "invalid_entity_refs"
    assert result.data["invalid_refs"][0]["entity_id"] == "light.does_not_exist"


# ─── add_section ────────────────────────────────────────────────────────────


async def test_add_section_appends_to_sections_view(tmp_path):
    ctx = _make_ctx(tmp_path, _sections_view_dashboard())
    result = await execute(
        "modify_dashboard",
        {
            "action": "add_section",
            "view_path": "rooms",
            "cards": [
                {"type": "heading", "heading": "Lights"},
                {"type": "tile", "entity": "light.kitchen_overhead"},
            ],
            "dry_run": False,
        },
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["section_count"] == 3

    view = ctx.ws_client.saved_config()["views"][0]
    assert len(view["sections"]) == 3
    assert view["sections"][2]["type"] == "grid"
    assert view["sections"][2]["cards"][0] == {"type": "heading", "heading": "Lights"}


async def test_add_section_with_config_dict(tmp_path):
    """A full section dict via config is used verbatim."""
    ctx = _make_ctx(tmp_path, _sections_view_dashboard())
    result = await execute(
        "modify_dashboard",
        {
            "action": "add_section",
            "view_path": "rooms",
            "config": {
                "type": "grid",
                "column_span": 2,
                "cards": [{"type": "tile", "entity": "sensor.kitchen_temp"}],
            },
            "dry_run": False,
        },
        ctx,
    )
    assert result.status.value == "ok", result
    view = ctx.ws_client.saved_config()["views"][0]
    assert view["sections"][2]["column_span"] == 2


async def test_add_section_on_masonry_view_errors(tmp_path):
    ctx = _make_ctx(tmp_path, _masonry_view_dashboard())
    result = await execute(
        "modify_dashboard",
        {
            "action": "add_section",
            "view_path": "legacy",
            "cards": [{"type": "tile", "entity": "light.kitchen_overhead"}],
            "dry_run": True,
        },
        ctx,
    )
    assert result.error_code == "not_sections_layout"


async def test_add_section_validates_refs(tmp_path):
    ctx = _make_ctx(tmp_path, _sections_view_dashboard())
    result = await execute(
        "modify_dashboard",
        {
            "action": "add_section",
            "view_path": "rooms",
            "cards": [{"type": "tile", "entity": "light.nope"}],
            "dry_run": True,
        },
        ctx,
    )
    assert result.error_code == "invalid_entity_refs"


# ─── update_view with sections ──────────────────────────────────────────────


async def test_update_view_validates_refs_inside_sections(tmp_path):
    """Regression: update_view only walked view.cards, so refs inside
    sections sailed through unvalidated."""
    ctx = _make_ctx(tmp_path, _sections_view_dashboard())
    result = await execute(
        "modify_dashboard",
        {
            "action": "update_view",
            "view_path": "rooms",
            "config": {
                "title": "Rooms",
                "type": "sections",
                "sections": [
                    {"type": "grid", "cards": [{"type": "tile", "entity": "light.ghost"}]}
                ],
            },
            "dry_run": True,
        },
        ctx,
    )
    assert result.error_code == "invalid_entity_refs"


async def test_update_view_sections_config_not_corrupted(tmp_path):
    """update_view with a sections replacement must not inject cards: []."""
    ctx = _make_ctx(tmp_path, _sections_view_dashboard())
    result = await execute(
        "modify_dashboard",
        {
            "action": "update_view",
            "view_path": "rooms",
            "config": {
                "title": "Rooms",
                "type": "sections",
                "sections": [
                    {
                        "type": "grid",
                        "cards": [{"type": "tile", "entity": "light.kitchen_overhead"}],
                    }
                ],
            },
            "dry_run": False,
        },
        ctx,
    )
    assert result.status.value == "ok", result
    view = ctx.ws_client.saved_config()["views"][0]
    assert "cards" not in view
    assert view["type"] == "sections"
