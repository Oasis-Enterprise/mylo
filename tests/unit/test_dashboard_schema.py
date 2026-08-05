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

"""Structural validation of Lovelace views (mylo.validators.dashboard_schema).

Structure only, on purpose: type present on every card, custom types
checked against installed resources, sane sections shape. Card *options*
are not validated — HA's card schemas churn too fast to chase.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.registries import EntityEntry, Registries
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from mylo.validators.dashboard_schema import validate_view
from tests.unit._helpers import make_ctx

_INSTALLED = {"custom:mushroom-light-card", "custom:mini-graph-card"}


def _sections_view(cards: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "title": "Test",
        "path": "test",
        "type": "sections",
        "sections": [{"type": "grid", "cards": cards}],
    }


# ─── validate_view ──────────────────────────────────────────────────────────


def test_valid_sections_view_passes():
    report = validate_view(
        _sections_view(
            [
                {"type": "heading", "heading": "Kitchen"},
                {"type": "tile", "entity": "light.kitchen"},
            ]
        ),
        installed_custom=_INSTALLED,
    )
    assert report.ok
    assert report.issues == []


def test_card_missing_type_errors():
    report = validate_view(_sections_view([{"entity": "light.kitchen"}]), installed_custom=None)
    assert not report.ok
    assert any("type" in i.message for i in report.issues if i.severity == "error")


def test_missing_type_found_in_nested_stack():
    view = {
        "title": "T",
        "cards": [
            {
                "type": "vertical-stack",
                "cards": [{"type": "horizontal-stack", "cards": [{"entity": "light.x"}]}],
            }
        ],
    }
    report = validate_view(view, installed_custom=None)
    assert not report.ok


def test_missing_type_found_inside_conditional_card():
    view = {
        "title": "T",
        "cards": [
            {
                "type": "conditional",
                "conditions": [{"entity": "cover.garage", "state": "open"}],
                "card": {"entity": "cover.garage"},
            }
        ],
    }
    report = validate_view(view, installed_custom=None)
    assert not report.ok


def test_unknown_native_type_warns_not_errors():
    report = validate_view(
        _sections_view([{"type": "brand-new-ha-card"}]), installed_custom=_INSTALLED
    )
    assert report.ok
    assert any(i.severity == "warning" for i in report.issues)


def test_uninstalled_custom_card_errors():
    report = validate_view(
        _sections_view([{"type": "custom:bubble-card", "entity": "light.x"}]),
        installed_custom=_INSTALLED,
    )
    assert not report.ok
    issue = next(i for i in report.issues if i.severity == "error")
    assert "custom:bubble-card" in issue.message
    assert "native" in issue.message


def test_installed_custom_card_passes():
    report = validate_view(
        _sections_view([{"type": "custom:mushroom-light-card", "entity": "light.x"}]),
        installed_custom=_INSTALLED,
    )
    assert report.ok


def test_custom_card_warns_when_resources_unknown():
    """installed_custom=None means we couldn't list resources — warn only."""
    report = validate_view(
        _sections_view([{"type": "custom:bubble-card"}]), installed_custom=None
    )
    assert report.ok
    assert any(i.severity == "warning" for i in report.issues)


def test_mixed_cards_and_sections_warns():
    view = {
        "title": "T",
        "type": "sections",
        "cards": [{"type": "tile", "entity": "light.x"}],
        "sections": [{"type": "grid", "cards": [{"type": "tile", "entity": "light.y"}]}],
    }
    report = validate_view(view, installed_custom=None)
    assert any("both" in i.message for i in report.issues if i.severity == "warning")


def test_section_not_a_dict_errors():
    view = {"title": "T", "type": "sections", "sections": ["oops"]}
    report = validate_view(view, installed_custom=None)
    assert not report.ok


def test_max_columns_out_of_range_warns():
    view = {"title": "T", "type": "sections", "max_columns": 12, "sections": []}
    report = validate_view(view, installed_custom=None)
    assert report.ok
    assert any("max_columns" in i.message for i in report.issues)


def test_stack_cards_not_a_list_errors():
    view = {"title": "T", "cards": [{"type": "vertical-stack", "cards": "nope"}]}
    report = validate_view(view, installed_custom=None)
    assert not report.ok


# ─── modify_dashboard integration ───────────────────────────────────────────


class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ in self._responses:
            val = self._responses[type_]
            if isinstance(val, Exception):
                raise val
            return val
        return {}


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


def _ctx(tmp_path: Path, responses: dict[str, Any]):
    reg = Registries()
    reg.entities = {
        "light.kitchen_overhead": EntityEntry.from_raw(
            {"entity_id": "light.kitchen_overhead", "original_name": "Kitchen"}
        ),
    }
    client = _FakeClient(responses)
    return make_ctx(ws_client=client, registries=reg, tmp_path=tmp_path)


async def test_create_blocks_on_schema_errors(tmp_path):
    ctx = _ctx(tmp_path, {"lovelace/config": {"views": []}})
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Kitchen",
            "path": "kitchen",
            "cards": [{"entity": "light.kitchen_overhead"}],
            "dry_run": True,
        },
        ctx,
    )
    assert result.error_code == "dashboard_schema_issues"
    assert result.data["issues"]


async def test_create_passes_warnings_into_preview(tmp_path):
    ctx = _ctx(
        tmp_path,
        {
            "lovelace/config": {"views": []},
            "lovelace/resources": [
                {"id": "1", "type": "module", "url": "/hacsfiles/lovelace-mushroom/mushroom.js"}
            ],
        },
    )
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Kitchen",
            "path": "kitchen",
            "cards": [{"type": "brand-new-ha-card", "entity": "light.kitchen_overhead"}],
            "dry_run": True,
        },
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["schema_warnings"]


async def test_create_blocks_uninstalled_custom_card(tmp_path):
    ctx = _ctx(
        tmp_path,
        {
            "lovelace/config": {"views": []},
            "lovelace/resources": [
                {"id": "1", "type": "module", "url": "/hacsfiles/lovelace-mushroom/mushroom.js"}
            ],
        },
    )
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Kitchen",
            "path": "kitchen",
            "cards": [{"type": "custom:bubble-card", "entity": "light.kitchen_overhead"}],
            "dry_run": True,
        },
        ctx,
    )
    assert result.error_code == "dashboard_schema_issues"


async def test_add_cards_validates_schema(tmp_path):
    ctx = _ctx(
        tmp_path,
        {
            "lovelace/config": {
                "views": [{"path": "home", "title": "Home", "cards": []}]
            },
        },
    )
    result = await execute(
        "modify_dashboard",
        {
            "action": "add_cards",
            "view_path": "home",
            "cards": [{"entity": "light.kitchen_overhead"}],
            "dry_run": True,
        },
        ctx,
    )
    assert result.error_code == "dashboard_schema_issues"
