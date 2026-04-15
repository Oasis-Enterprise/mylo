"""Tests for tier-2 write tools + permission gate dry-run behavior.

Each tool is driven through the executor so the permission gate and
audit log fire the same way they would at runtime. A FakeClient stands
in for HA.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute
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
def _ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "config").mkdir()

    reg = Registries()
    reg.areas = {
        "kitchen": AreaEntry.from_raw({"area_id": "kitchen", "name": "Kitchen"}),
    }
    reg.entities = {
        "light.kitchen_overhead": EntityEntry.from_raw(
            {
                "entity_id": "light.kitchen_overhead",
                "area_id": "kitchen",
                "labels": [],
            }
        ),
    }
    reg.devices = {
        "d1": DeviceEntry.from_raw({"id": "d1", "area_id": "kitchen"}),
    }

    client = _FakeClient(states=[{"entity_id": "automation.mylo_morning", "state": "on"}])
    ctx = make_ctx(ws_client=client, registries=reg, tmp_path=tmp_path / "config")
    # Override mylo_data_dir to keep it inside tmp.
    ctx.config.__class__.mylo_data_dir.__set__(ctx.config, tmp_path / ".mylo")  # type: ignore[attr-defined]
    return ctx


@pytest.fixture(autouse=True)
def _load_tools() -> Any:
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


# ─── permission gate: dry-run allowed without approval ─────────────────────


async def test_dry_run_write_allowed_without_approval(_ctx: ToolContext) -> None:
    _ctx.user_approved = False
    result = await execute(
        "write_config_file",
        {
            "path": "packages/agent.yaml",
            "content": "automation: []\n",
            "domain": "automation",
            "dry_run": True,
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True


async def test_non_dry_run_write_blocked_without_approval(_ctx: ToolContext) -> None:
    _ctx.user_approved = False
    result = await execute(
        "write_config_file",
        {
            "path": "packages/agent.yaml",
            "content": "automation: []\n",
            "domain": "automation",
            "dry_run": False,
        },
        _ctx,
    )
    assert result.status.value == "error"
    assert result.error_code == "confirmation_required"


# ─── write_config_file: path policy + schema + diff ─────────────────────────


async def test_write_config_file_rejects_blocked_path(_ctx: ToolContext) -> None:
    result = await execute(
        "write_config_file",
        {"path": "configuration.yaml", "content": "x: 1\n", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "denied_write"


async def test_write_config_file_rejects_bad_yaml(_ctx: ToolContext) -> None:
    # Mismatched flow collection — ruamel actually errors on this.
    result = await execute(
        "write_config_file",
        {"path": "packages/agent.yaml", "content": "foo: {bar: [baz\n", "dry_run": True},
        _ctx,
    )
    assert result.error_code == "invalid_yaml"


async def test_write_config_file_catches_schema_issue(_ctx: ToolContext) -> None:
    # A dict with 'trigger' but missing 'action' — schema validator should
    # flag it.
    content = (
        "automation:\n"
        "  - id: mylo_test\n"
        "    alias: Test\n"
        "    trigger:\n"
        "      - platform: time\n"
        "        at: '07:00'\n"
    )
    result = await execute(
        "write_config_file",
        {"path": "packages/agent.yaml", "content": content, "dry_run": True},
        _ctx,
    )
    assert result.error_code == "schema_invalid"


async def test_write_config_file_catches_unknown_entity(_ctx: ToolContext) -> None:
    content = (
        "automation:\n"
        "  - id: mylo_test\n"
        "    alias: Test\n"
        "    trigger:\n"
        "      - platform: state\n"
        "        entity_id: sensor.does_not_exist\n"
        "    action:\n"
        "      - service: light.turn_on\n"
        "        target:\n"
        "          entity_id: light.kitchen_overhead\n"
    )
    result = await execute(
        "write_config_file",
        {"path": "packages/agent.yaml", "content": content, "dry_run": True},
        _ctx,
    )
    assert result.error_code == "references_invalid"
    assert any(
        m["invalid_ref"] == "sensor.does_not_exist" for m in result.data["ref_check"]["mismatches"]
    )


async def test_write_config_file_dry_run_returns_diff(_ctx: ToolContext) -> None:
    content = (
        "automation:\n"
        "  - id: mylo_test\n"
        "    alias: Test\n"
        "    trigger:\n"
        "      - platform: state\n"
        "        entity_id: light.kitchen_overhead\n"
        "    action:\n"
        "      - service: light.turn_off\n"
        "        target:\n"
        "          entity_id: light.kitchen_overhead\n"
    )
    result = await execute(
        "write_config_file",
        {"path": "packages/agent.yaml", "content": content, "dry_run": True},
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["mode"] == "create"
    assert result.data["diff"]["summary"]["added"] >= 1


# ─── modify_automation: the full dry-run → apply → verify path ─────────────


async def test_modify_automation_create_dry_run(_ctx: ToolContext) -> None:
    config = {
        "alias": "Morning Routine",
        "trigger": [{"platform": "time", "at": "07:00"}],
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.kitchen_overhead"}}],
    }
    result = await execute(
        "modify_automation",
        {"action": "create", "config": config, "dry_run": True},
        _ctx,
    )
    assert result.status.value == "ok"
    assert result.data["preview"] is True
    assert result.data["automation_id"].startswith("mylo_")


async def test_modify_automation_update_requires_existing_id(_ctx: ToolContext) -> None:
    result = await execute(
        "modify_automation",
        {
            "action": "update",
            "automation_id": "mylo_missing",
            "config": {
                "alias": "x",
                "trigger": [{"platform": "time", "at": "07:00"}],
                "action": [{"service": "light.turn_on"}],
            },
            "dry_run": True,
        },
        _ctx,
    )
    assert result.error_code == "not_found"


async def test_call_service_blocked(_ctx: ToolContext) -> None:
    _ctx.user_approved = True
    result = await execute(
        "call_service",
        {"domain": "homeassistant", "service": "restart"},
        _ctx,
    )
    assert result.error_code == "service_blocked"


async def test_call_service_without_approval_blocked(_ctx: ToolContext) -> None:
    _ctx.user_approved = False
    result = await execute(
        "call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen_overhead"},
        },
        _ctx,
    )
    assert result.error_code == "confirmation_required"


async def test_call_service_with_approval_invokes_ha(_ctx: ToolContext) -> None:
    _ctx.user_approved = True
    client: _FakeClient = _ctx.ws_client  # type: ignore[assignment]
    result = await execute(
        "call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen_overhead"},
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert (
        "call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen_overhead"},
            "service_data": None,
        },
    ) in client.calls


async def test_call_service_restricted_service_adds_warning(_ctx: ToolContext) -> None:
    _ctx.user_approved = True
    result = await execute(
        "call_service",
        {
            "domain": "lock",
            "service": "unlock",
            "target": {"entity_id": "lock.front_door"},
        },
        _ctx,
    )
    assert result.status.value == "ok"
    assert "warning" in result.data
    assert "unlock" in result.data["warning"].lower()


async def test_reload_config_valid_scope(_ctx: ToolContext) -> None:
    _ctx.user_approved = True
    result = await execute("reload_config", {"scope": "automation"}, _ctx)
    assert result.status.value == "ok"


async def test_reload_config_unknown_scope(_ctx: ToolContext) -> None:
    _ctx.user_approved = True
    result = await execute("reload_config", {"scope": "not_a_scope"}, _ctx)
    assert result.error_code == "unknown_scope"
