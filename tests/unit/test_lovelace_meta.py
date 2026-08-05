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

"""Themes & lovelace-resources discovery (mylo.ha.lovelace_meta) and the
query_dashboard_env read tool.

Before this existed, Mylo guessed at themes (blind string pass-through)
and used custom cards with no way to know whether the HACS resources
were installed — silently rendering "Custom element doesn't exist".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.lovelace_meta import detect_custom_cards, get_resources, get_themes
from mylo.ha.registries import Registries
from mylo.ha.ws_client import CommandError
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
            if isinstance(val, Exception):
                raise val
            return val
        return {}


_THEMES_RESPONSE = {
    "themes": {
        "noctis": {"primary-color": "#5294e2"},
        "ios-dark-mode": {"primary-color": "#000"},
    },
    "default_theme": "noctis",
    "default_dark_theme": None,
}

_RESOURCES_RESPONSE = [
    {"id": "1", "type": "module", "url": "/hacsfiles/lovelace-mushroom/mushroom.js"},
    {"id": "2", "type": "module", "url": "/hacsfiles/mini-graph-card/mini-graph-card-bundle.js"},
    {"id": "3", "type": "module", "url": "/hacsfiles/weird-thing/weird-thing.js"},
]


# ─── lovelace_meta helpers ──────────────────────────────────────────────────


async def test_get_themes_returns_names_and_default():
    client = _FakeClient({"frontend/get_themes": _THEMES_RESPONSE})
    themes = await get_themes(client)
    assert themes is not None
    assert sorted(themes.names) == ["ios-dark-mode", "noctis"]
    assert themes.default == "noctis"


async def test_get_themes_none_on_command_error():
    client = _FakeClient({"frontend/get_themes": CommandError("unknown_command", "nope")})
    assert await get_themes(client) is None


async def test_get_resources_returns_list():
    client = _FakeClient({"lovelace/resources": _RESOURCES_RESPONSE})
    resources = await get_resources(client)
    assert resources is not None
    assert len(resources) == 3


async def test_get_resources_none_on_command_error():
    """YAML-mode resources aren't listable — degrade to None, not a crash."""
    client = _FakeClient({"lovelace/resources": CommandError("unknown_command", "nope")})
    assert await get_resources(client) is None


def test_detect_custom_cards_known_families():
    cards = detect_custom_cards(_RESOURCES_RESPONSE)
    assert "custom:mushroom-light-card" in cards
    assert "custom:mushroom-title-card" in cards
    assert "custom:mini-graph-card" in cards
    # Unknown resource falls back to its basename as a card element.
    assert "custom:weird-thing" in cards


def test_detect_custom_cards_empty_input():
    assert detect_custom_cards([]) == set()
    assert detect_custom_cards(None) == set()


# ─── query_dashboard_env tool ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _load_tools():
    from mylo.tools import executor

    tool_registry._reset_for_tests()
    tool_registry.load_all()
    # query_dashboard_env takes no params, so every call shares one
    # executor-cache key — clear it or tests see each other's results.
    executor._result_cache.clear()
    yield
    tool_registry._reset_for_tests()
    executor._result_cache.clear()


def _env_ctx(tmp_path: Path, responses: dict[str, Any]):
    client = _FakeClient(responses)
    return make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path)


async def test_query_dashboard_env_reports_themes_and_cards(tmp_path):
    ctx = _env_ctx(
        tmp_path,
        {
            "frontend/get_themes": _THEMES_RESPONSE,
            "lovelace/resources": _RESOURCES_RESPONSE,
        },
    )
    result = await execute("query_dashboard_env", {}, ctx)
    assert result.status.value == "ok", result
    assert sorted(result.data["themes"]) == ["ios-dark-mode", "noctis"]
    assert result.data["default_theme"] == "noctis"
    assert result.data["resource_count"] == 3
    assert "custom:mushroom-light-card" in result.data["custom_cards_detected"]


async def test_query_dashboard_env_degrades_when_unavailable(tmp_path):
    ctx = _env_ctx(
        tmp_path,
        {
            "frontend/get_themes": CommandError("unknown_command", "nope"),
            "lovelace/resources": CommandError("unknown_command", "nope"),
        },
    )
    result = await execute("query_dashboard_env", {}, ctx)
    assert result.status.value == "ok", result
    assert result.data["themes"] is None
    assert result.data["custom_cards_detected"] is None


# ─── modify_dashboard theme validation ──────────────────────────────────────


async def test_create_with_unknown_theme_errors(tmp_path):
    ctx = _env_ctx(
        tmp_path,
        {
            "frontend/get_themes": _THEMES_RESPONSE,
            "lovelace/config": {"views": []},
        },
    )
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Kitchen",
            "path": "kitchen",
            "theme": "solarized",
            "dry_run": True,
        },
        ctx,
    )
    assert result.error_code == "invalid_theme"
    assert sorted(result.data["available_themes"]) == ["ios-dark-mode", "noctis"]


async def test_create_with_installed_theme_passes(tmp_path):
    ctx = _env_ctx(
        tmp_path,
        {
            "frontend/get_themes": _THEMES_RESPONSE,
            "lovelace/config": {"views": []},
        },
    )
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Kitchen",
            "path": "kitchen",
            "theme": "noctis",
            "dry_run": True,
        },
        ctx,
    )
    assert result.status.value == "ok", result


async def test_theme_check_skipped_when_themes_unavailable(tmp_path):
    """If frontend/get_themes fails we can't validate — let it through."""
    ctx = _env_ctx(
        tmp_path,
        {
            "frontend/get_themes": CommandError("unknown_command", "nope"),
            "lovelace/config": {"views": []},
        },
    )
    result = await execute(
        "modify_dashboard",
        {
            "action": "create",
            "title": "Kitchen",
            "path": "kitchen",
            "theme": "solarized",
            "dry_run": True,
        },
        ctx,
    )
    assert result.status.value == "ok", result
