"""plan_dashboard: fetch, validate, stage, and the error envelopes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.dashboard.store import PlanStore
from mylo.ha.registries import EntityEntry, Registries
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
            if isinstance(val, Exception):
                raise val
            if callable(val):
                return val(**kwargs)
            return val
        return {}


def _registries() -> Registries:
    reg = Registries()
    reg.entities = {
        e: EntityEntry.from_raw({"entity_id": e, "original_name": e})
        for e in ("light.kitchen", "sensor.temp")
    }
    return reg


_DASHBOARD = {
    "views": [
        {
            "path": "rooms",
            "title": "Rooms",
            "type": "sections",
            "sections": [
                {"type": "grid", "cards": [{"type": "heading", "heading": "Lights"}]},
            ],
        }
    ]
}


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


def _ctx(
    tmp_path: Path, responses: dict[str, Any] | None = None, *, plans: PlanStore | None = None
):
    client = _FakeClient({"lovelace/config": _DASHBOARD, **(responses or {})})
    return make_ctx(
        ws_client=client, registries=_registries(), tmp_path=tmp_path, plans=plans or PlanStore()
    )


def _params(*ops: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"summary": "test plan", "operations": list(ops), **extra}


async def test_valid_plan_is_staged(tmp_path: Path) -> None:
    store = PlanStore()
    ctx = _ctx(tmp_path, plans=store)
    result = await execute(
        "plan_dashboard",
        _params(
            {
                "op": "add_cards",
                "view_path": "rooms",
                "section": "Lights",
                "cards": [{"type": "tile", "entity": "light.kitchen"}],
            },
            assumptions=["only kitchen entities"],
        ),
        ctx,
    )
    assert result.status.value == "ok", result.error_message
    data = result.data
    assert data["preview"] is True
    assert data["entity_refs_validated"] == 1
    assert data["plan"]["assumptions"] == ["only kitchen entities"]
    assert data["plan"]["resolved"][0]["section_index"] == 0
    assert store.get(data["plan_id"]) is not None
    assert not any(t == "lovelace/config/save" for t, _ in ctx.ws_client.calls)


async def test_invalid_plan_not_staged(tmp_path: Path) -> None:
    store = PlanStore()
    ctx = _ctx(tmp_path, plans=store)
    result = await execute(
        "plan_dashboard",
        _params(
            {
                "op": "add_cards",
                "view_path": "rooms",
                "section": "Lights",
                "cards": [{"type": "tile", "entity": "light.kitchn"}],
            }
        ),
        ctx,
    )
    assert result.error_code == "plan_invalid"
    assert result.data["issues"][0]["code"] == "invalid_entity_refs"
    assert "light.kitchen" in result.data["issues"][0]["message"]
    assert store._entries == {}


async def test_two_calls_get_distinct_plan_ids(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    params = _params({"op": "delete_view", "view_path": "rooms"})
    a = await execute("plan_dashboard", params, ctx)
    b = await execute("plan_dashboard", params, ctx)
    assert a.data["plan_id"] != b.data["plan_id"]


async def test_named_dashboard_not_found(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"lovelace/config": CommandError("config_not_found", "nope")})
    result = await execute(
        "plan_dashboard",
        _params({"op": "create_view", "title": "K", "path": "k"}, dashboard_id="tablet"),
        ctx,
    )
    assert result.error_code == "dashboard_not_found"


async def test_default_dashboard_without_config_plans_against_empty(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"lovelace/config": CommandError("config_not_found", "nope")})
    result = await execute(
        "plan_dashboard", _params({"op": "create_view", "title": "K", "path": "k"}), ctx
    )
    assert result.status.value == "ok"


async def test_theme_checked_when_listable(tmp_path: Path) -> None:
    themes = {"themes": {"noctis": {}}, "default_theme": "noctis"}
    ctx = _ctx(tmp_path, {"frontend/get_themes": themes})
    bad = await execute(
        "plan_dashboard",
        _params({"op": "create_view", "title": "K", "path": "k", "theme": "ios"}),
        ctx,
    )
    assert bad.error_code == "plan_invalid"
    assert bad.data["issues"][0]["code"] == "theme_not_installed"
    good = await execute(
        "plan_dashboard",
        _params({"op": "create_view", "title": "K", "path": "k", "theme": "noctis"}),
        ctx,
    )
    assert good.status.value == "ok"


async def test_theme_skipped_when_unavailable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"frontend/get_themes": CommandError("unknown_command", "no")})
    result = await execute(
        "plan_dashboard",
        _params({"op": "create_view", "title": "K", "path": "k", "theme": "anything"}),
        ctx,
    )
    assert result.status.value == "ok"


async def test_custom_card_triggers_resource_lookup(tmp_path: Path) -> None:
    resources = [{"url": "/hacsfiles/mushroom/mushroom.js", "type": "module"}]
    ctx = _ctx(tmp_path, {"lovelace/resources": resources})
    result = await execute(
        "plan_dashboard",
        _params(
            {
                "op": "add_cards",
                "view_path": "rooms",
                "section": 0,
                "cards": [{"type": "custom:nope-card", "entity": "light.kitchen"}],
            }
        ),
        ctx,
    )
    assert result.error_code == "plan_invalid"
    assert any(t == "lovelace/resources" for t, _ in ctx.ws_client.calls)


async def test_native_only_plan_skips_resource_lookup(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    await execute(
        "plan_dashboard",
        _params(
            {
                "op": "add_cards",
                "view_path": "rooms",
                "section": 0,
                "cards": [{"type": "tile", "entity": "light.kitchen"}],
            }
        ),
        ctx,
    )
    assert not any(t == "lovelace/resources" for t, _ in ctx.ws_client.calls)
