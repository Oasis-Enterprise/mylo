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

"""Tests for modify_zones — create/update/delete HA zones.

Mirrors the structure of test_modify_scene.py. All tests drive through the
executor so the permission gate fires the same way it does at runtime.
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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        return None


@pytest.fixture
def _client() -> _FakeClient:
    return _FakeClient()


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
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    assert result.data["zone_id"] is not None
    # diff shows something was added
    assert result.data["diff"]["summary"]["added"] >= 1


async def test_create_dry_run_writes_nothing(_ctx: ToolContext, tmp_path: Path) -> None:
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert not pkg_path.exists()


# ─── create: writes zone entry when approved ─────────────────────────────────


async def test_create_writes_zone_entry(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True
    result = await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "radius": 150.0,
            "icon": "mdi:briefcase",
            "passive": False,
            "dry_run": False,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is False

    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    assert pkg_path.exists()
    parsed = load_yaml(pkg_path.read_text())
    assert isinstance(parsed, dict)
    zones = parsed.get("zone", [])
    assert isinstance(zones, list)
    assert len(zones) == 1
    zone = zones[0]
    assert zone["name"] == "Work"
    assert zone["latitude"] == 37.4
    assert zone["longitude"] == -122.1
    assert zone["radius"] == 150.0
    assert zone["icon"] == "mdi:briefcase"
    assert zone["passive"] is False
    assert "id" in zone


async def test_create_uses_default_radius(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    """radius defaults to 100 when not supplied."""
    _ctx.user_approved = True
    await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "School",
            "latitude": 38.0,
            "longitude": -121.0,
            "dry_run": False,
        },
        _ctx,
    )
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    assert parsed["zone"][0]["radius"] == 100.0


# ─── create: missing required params ─────────────────────────────────────────


async def test_create_missing_latitude_returns_error(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "missing_param"


async def test_create_missing_longitude_returns_error(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "missing_param"


async def test_create_missing_name_returns_error(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {
            "action": "create",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code in ("missing_param", "invalid_params")


# ─── update ──────────────────────────────────────────────────────────────────


async def test_update_replaces_zone_entry(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True

    # First create
    await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": False,
        },
        _ctx,
    )

    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    zone_id = parsed["zone"][0]["id"]

    # Now update
    result = await execute(
        "modify_zones",
        {
            "action": "update",
            "zone_id": zone_id,
            "name": "Work HQ",
            "latitude": 37.5,
            "longitude": -122.2,
            "radius": 200.0,
            "dry_run": False,
        },
        _ctx,
    )
    assert result.status.value == "ok"

    parsed2 = load_yaml(pkg_path.read_text())
    zones = parsed2["zone"]
    assert len(zones) == 1
    assert zones[0]["name"] == "Work HQ"
    assert zones[0]["latitude"] == 37.5
    assert zones[0]["radius"] == 200.0


async def test_update_not_found_returns_error(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {
            "action": "update",
            "zone_id": "nonexistent_zone",
            "name": "Whatever",
            "latitude": 37.4,
            "longitude": -122.1,
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
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": False,
        },
        _ctx,
    )

    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    zone_id = parsed["zone"][0]["id"]

    result = await execute(
        "modify_zones",
        {
            "action": "update",
            "zone_id": zone_id,
            "name": "Work Updated",
            "latitude": 37.5,
            "longitude": -122.2,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True


async def test_update_requires_zone_id(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {
            "action": "update",
            "name": "New Name",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "missing_param"


# ─── delete ──────────────────────────────────────────────────────────────────


async def test_delete_removes_zone_entry(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True

    await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": False,
        },
        _ctx,
    )
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    zone_id = parsed["zone"][0]["id"]

    result = await execute(
        "modify_zones",
        {"action": "delete", "zone_id": zone_id, "dry_run": False},
        _ctx,
    )
    assert result.status.value == "ok"

    parsed2 = load_yaml(pkg_path.read_text())
    assert parsed2.get("zone", []) == []


async def test_delete_not_found_returns_error(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {"action": "delete", "zone_id": "does_not_exist", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "not_found"


async def test_delete_dry_run_preview(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    _ctx.user_approved = True
    await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": False,
        },
        _ctx,
    )
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    parsed = load_yaml(pkg_path.read_text())
    zone_id = parsed["zone"][0]["id"]

    result = await execute(
        "modify_zones",
        {"action": "delete", "zone_id": zone_id, "dry_run": True},
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    # File should still have the zone (dry run didn't delete)
    parsed2 = load_yaml(pkg_path.read_text())
    assert len(parsed2["zone"]) == 1


async def test_delete_requires_zone_id(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {"action": "delete", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "missing_param"


# ─── home zone guard ─────────────────────────────────────────────────────────


async def test_create_home_zone_rejected(_ctx: ToolContext) -> None:
    """Creating a zone named 'home' (case-insensitive) must be rejected."""
    result = await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Home",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "forbidden"


async def test_create_home_zone_lowercase_rejected(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "home",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "forbidden"


async def test_update_home_zone_rejected(_ctx: ToolContext) -> None:
    """Updating the built-in home zone must be rejected."""
    result = await execute(
        "modify_zones",
        {
            "action": "update",
            "zone_id": "home",
            "name": "My Home",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "forbidden"


async def test_update_home_zone_case_insensitive_rejected(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {
            "action": "update",
            "zone_id": "HOME",
            "name": "Home",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "forbidden"


async def test_delete_home_zone_rejected(_ctx: ToolContext) -> None:
    """Deleting the built-in home zone must be rejected."""
    result = await execute(
        "modify_zones",
        {"action": "delete", "zone_id": "home", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "forbidden"


async def test_delete_home_zone_case_insensitive_rejected(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_zones",
        {"action": "delete", "zone_id": "Home", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "forbidden"


# ─── non-dry-run blocked without approval ────────────────────────────────────


async def test_create_blocked_without_approval(_ctx: ToolContext) -> None:
    _ctx.user_approved = False
    result = await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": False,
        },
        _ctx,
    )
    assert result.error_code == "confirmation_required"


async def test_delete_blocked_without_approval(
    _ctx: ToolContext, tmp_path: Path, _client: _FakeClient
) -> None:
    """Attempting delete with dry_run=false but no approval must be blocked."""
    _ctx.user_approved = True
    await execute(
        "modify_zones",
        {
            "action": "create",
            "name": "Work",
            "latitude": 37.4,
            "longitude": -122.1,
            "dry_run": False,
        },
        _ctx,
    )
    pkg_path = tmp_path / "config" / "packages" / "agent.yaml"
    zone_id = load_yaml(pkg_path.read_text())["zone"][0]["id"]

    _ctx.user_approved = False
    result = await execute(
        "modify_zones",
        {"action": "delete", "zone_id": zone_id, "dry_run": False},
        _ctx,
    )
    assert result.error_code == "confirmation_required"


# ─── duplicate create ─────────────────────────────────────────────────────────


async def test_create_duplicate_id_returns_error(_ctx: ToolContext, _client: _FakeClient) -> None:
    _ctx.user_approved = True
    params = {
        "action": "create",
        "name": "Work",
        "latitude": 37.4,
        "longitude": -122.1,
        "dry_run": False,
    }
    await execute("modify_zones", params, _ctx)

    # Second create with same name (same derived id)
    result = await execute("modify_zones", {**params, "dry_run": True}, _ctx)
    assert result.error_code == "already_exists"
