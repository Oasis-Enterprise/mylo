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

"""Tests for modify_scene — create/update/delete/activate HA scenes.

Mirrors the structure used in test_write_tools.py for modify_automation.
All tests drive through the executor so the permission gate fires the
same way it does at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.registries import Registries
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute
from mylo.validators.yaml_parser import load_yaml
from tests.unit._helpers import make_ctx


class _FakeClient:
    def __init__(self, states: list[dict[str, Any]] | None = None) -> None:
        self._states = states or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "get_states":
            return self._states
        return None


@pytest.fixture
def _client() -> _FakeClient:
    return _FakeClient(
        states=[
            {
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"brightness": 200, "color_temp": 300},
            },
            {
                "entity_id": "switch.fan",
                "state": "off",
                "attributes": {},
            },
        ]
    )


@pytest.fixture
def _ctx(tmp_path: Path, _client: _FakeClient) -> ToolContext:
    (tmp_path / "config").mkdir()
    ctx = make_ctx(
        ws_client=_client,
        registries=Registries(),
        tmp_path=tmp_path / "config",
    )
    return ctx


@pytest.fixture(autouse=True)
def _load_tools() -> Any:
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


# ─── create: dry_run preview ──────────────────────────────────────────────────


async def test_create_dry_run_returns_preview(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Evening Chill",
            "entities": {
                "light.kitchen": {"state": "on", "brightness": 100},
                "switch.fan": "off",
            },
            "dry_run": True,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    assert result.data["scene_id"] is not None
    # diff shows something was added
    assert result.data["diff"]["summary"]["added"] >= 1


async def test_create_dry_run_writes_nothing(_ctx: ToolContext, tmp_path: Path) -> None:
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Evening Chill",
            "entities": {"light.kitchen": {"state": "on"}},
            "dry_run": True,
        },
        _ctx,
    )
    assert not pkg_path.exists()


# ─── create: writes the scene entry when approved ────────────────────────────


async def test_create_writes_scene_entry(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True
    result = await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Evening Chill",
            "entities": {
                "light.kitchen": {"state": "on", "brightness": 100},
                "switch.fan": "off",
            },
            "dry_run": False,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is False

    # File must exist and contain the scene entry
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    assert pkg_path.exists()
    parsed = load_yaml(pkg_path.read_text())
    assert isinstance(parsed, dict)
    scenes = parsed.get("scene", [])
    assert isinstance(scenes, list)
    assert len(scenes) == 1
    scene = scenes[0]
    assert scene["name"] == "Evening Chill"
    assert "entities" in scene


# ─── create with capture_entities ────────────────────────────────────────────


async def test_create_capture_entities_snapshots_states(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    """capture_entities should snapshot current state+attributes from ws."""
    _ctx.user_approved = True
    result = await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Snapshot Scene",
            "capture_entities": ["light.kitchen", "switch.fan"],
            "dry_run": False,
        },
        _ctx,
    )
    assert result.status.value == "ok"

    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    scenes = parsed["scene"]
    scene = scenes[0]
    entities = scene["entities"]

    # light.kitchen: should have state + attributes captured
    assert "light.kitchen" in entities
    assert "switch.fan" in entities
    # ws get_states was called
    assert any(t == "get_states" for t, _ in _client.calls)


async def test_create_capture_entities_dry_run_preview(
    _ctx: ToolContext, _client: _FakeClient
) -> None:
    """capture_entities in dry_run mode should preview the snapshot, not write."""
    result = await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Snapshot Scene",
            "capture_entities": ["light.kitchen"],
            "dry_run": True,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    # States were fetched
    assert any(t == "get_states" for t, _ in _client.calls)


# ─── update ──────────────────────────────────────────────────────────────────


async def test_update_replaces_scene_entry(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True

    # First create
    await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Evening Chill",
            "entities": {"light.kitchen": {"state": "on", "brightness": 100}},
            "dry_run": False,
        },
        _ctx,
    )

    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    scene_id = parsed["scene"][0]["id"]

    # Now update
    result = await execute(
        "modify_scene",
        {
            "action": "update",
            "scene_id": scene_id,
            "name": "Evening Chill v2",
            "entities": {"light.kitchen": {"state": "on", "brightness": 50}},
            "dry_run": False,
        },
        _ctx,
    )
    assert result.status.value == "ok"

    parsed2 = load_yaml(pkg_path.read_text())
    scenes = parsed2["scene"]
    assert len(scenes) == 1
    assert scenes[0]["name"] == "Evening Chill v2"


async def test_update_not_found_returns_error(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_scene",
        {
            "action": "update",
            "scene_id": "nonexistent_scene",
            "name": "Whatever",
            "entities": {"light.kitchen": "on"},
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "not_found"


async def test_update_dry_run_preview(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True

    await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Evening Chill",
            "entities": {"light.kitchen": {"state": "on"}},
            "dry_run": False,
        },
        _ctx,
    )

    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    scene_id = parsed["scene"][0]["id"]

    result = await execute(
        "modify_scene",
        {
            "action": "update",
            "scene_id": scene_id,
            "name": "Updated Name",
            "entities": {"light.kitchen": {"state": "on", "brightness": 200}},
            "dry_run": True,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True


# ─── delete ──────────────────────────────────────────────────────────────────


async def test_delete_removes_scene_entry(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True

    await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Evening Chill",
            "entities": {"light.kitchen": "on"},
            "dry_run": False,
        },
        _ctx,
    )
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    scene_id = parsed["scene"][0]["id"]

    result = await execute(
        "modify_scene",
        {"action": "delete", "scene_id": scene_id, "dry_run": False},
        _ctx,
    )
    assert result.status.value == "ok"

    parsed2 = load_yaml(pkg_path.read_text())
    assert parsed2.get("scene", []) == []


async def test_delete_not_found_returns_error(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_scene",
        {"action": "delete", "scene_id": "does_not_exist", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "not_found"


async def test_delete_dry_run_preview(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True
    await execute(
        "modify_scene",
        {
            "action": "create",
            "name": "Evening Chill",
            "entities": {"light.kitchen": "on"},
            "dry_run": False,
        },
        _ctx,
    )
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    scene_id = parsed["scene"][0]["id"]

    result = await execute(
        "modify_scene",
        {"action": "delete", "scene_id": scene_id, "dry_run": True},
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    # File should still have the scene (dry run didn't delete)
    parsed2 = load_yaml(pkg_path.read_text())
    assert len(parsed2["scene"]) == 1


# ─── activate ────────────────────────────────────────────────────────────────


async def test_activate_calls_scene_turn_on(_ctx: ToolContext, _client: _FakeClient) -> None:
    _ctx.user_approved = True
    result = await execute(
        "modify_scene",
        {"action": "activate", "scene_id": "evening_chill", "dry_run": False},
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data.get("activated") == "scene.evening_chill"

    # ws_client should have received a call_service for scene.turn_on
    service_calls = [(t, kw) for t, kw in _client.calls if t == "call_service"]
    assert len(service_calls) == 1
    _, kw = service_calls[0]
    assert kw["domain"] == "scene"
    assert kw["service"] == "turn_on"
    assert kw["target"] == {"entity_id": "scene.evening_chill"}


async def test_activate_blocked_without_approval(_ctx: ToolContext) -> None:
    _ctx.user_approved = False
    result = await execute(
        "modify_scene",
        {"action": "activate", "scene_id": "evening_chill", "dry_run": False},
        _ctx,
    )
    assert result.error_code == "confirmation_required"


async def test_activate_dry_run_returns_preview(_ctx: ToolContext, _client: _FakeClient) -> None:
    """activate with dry_run=True should describe what it would do without calling HA."""
    result = await execute(
        "modify_scene",
        {"action": "activate", "scene_id": "evening_chill", "dry_run": True},
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    # Should NOT have called scene.turn_on
    service_calls = [t for t, _ in _client.calls if t == "call_service"]
    assert len(service_calls) == 0


# ─── validation errors ────────────────────────────────────────────────────────


async def test_create_requires_name(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_scene",
        {
            "action": "create",
            "entities": {"light.kitchen": "on"},
            "dry_run": True,
        },
        _ctx,
    )
    # Should error — name is required for create
    assert result.error_code in ("missing_param", "invalid_params")


async def test_update_requires_scene_id(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_scene",
        {
            "action": "update",
            "name": "New Name",
            "entities": {"light.kitchen": "on"},
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "missing_param"


async def test_delete_requires_scene_id(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_scene",
        {"action": "delete", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "missing_param"


async def test_activate_requires_scene_id(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_scene",
        {"action": "activate", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "missing_param"


# ─── already exists ───────────────────────────────────────────────────────────


async def test_create_duplicate_id_returns_error(_ctx: ToolContext, _client: _FakeClient) -> None:
    _ctx.user_approved = True
    params = {
        "action": "create",
        "name": "Evening Chill",
        "entities": {"light.kitchen": "on"},
        "dry_run": False,
    }
    await execute("modify_scene", params, _ctx)

    # Second create with same name (same derived id)
    result = await execute("modify_scene", {**params, "dry_run": True}, _ctx)
    assert result.error_code == "already_exists"
