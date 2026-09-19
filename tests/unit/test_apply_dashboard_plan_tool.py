"""apply_dashboard_plan: approval binding, single save, backup, verify."""

from __future__ import annotations

import copy
import json
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
    """Returns a live config that the save command updates, so read-back
    after save reflects the write (or a tampered version of it)."""

    def __init__(
        self, config: dict[str, Any], *, save_raises: Exception | None = None, tamper=None
    ) -> None:
        self.config = config
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._save_raises = save_raises
        self._tamper = tamper

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "lovelace/config":
            return copy.deepcopy(self.config)
        if type_ == "lovelace/config/save":
            if self._save_raises is not None:
                raise self._save_raises
            saved = copy.deepcopy(kwargs["config"])
            self.config = self._tamper(saved) if self._tamper else saved
            return None
        return {}

    def saves(self) -> list[dict[str, Any]]:
        return [k["config"] for t, k in self.calls if t == "lovelace/config/save"]


def _registries() -> Registries:
    reg = Registries()
    reg.entities = {
        e: EntityEntry.from_raw({"entity_id": e}) for e in ("light.kitchen", "light.hall")
    }
    return reg


def _dashboard() -> dict[str, Any]:
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
                            {"type": "heading", "heading": "Lights"},
                            {"type": "tile", "entity": "light.kitchen"},
                        ],
                    }
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


async def _staged(
    tmp_path: Path, client: _FakeClient, store: PlanStore, ops: list[dict[str, Any]]
) -> str:
    ctx = make_ctx(ws_client=client, registries=_registries(), tmp_path=tmp_path, plans=store)
    result = await execute("plan_dashboard", {"summary": "s", "operations": ops}, ctx)
    assert result.status.value == "ok", result.data
    return result.data["plan_id"]


def _apply_ctx(tmp_path: Path, client: _FakeClient, store: PlanStore, plan_id: str, **kw: Any):
    return make_ctx(
        ws_client=client,
        registries=_registries(),
        tmp_path=tmp_path,
        plans=store,
        user_approved=kw.pop("user_approved", True),
        approved_plan_ids=kw.pop("approved_plan_ids", frozenset({plan_id})),
        **kw,
    )


OPS = [
    {
        "op": "add_cards",
        "view_path": "rooms",
        "section": "Lights",
        "cards": [{"type": "tile", "entity": "light.hall"}],
    },
    {"op": "create_view", "title": "K", "path": "k", "sections": [{"heading": "A", "cards": []}]},
]


async def test_apply_happy_path(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.status.value == "ok", result.error_message
    data = result.data
    assert data["preview"] is False
    assert data["applied"] == 2
    assert data["verification"]["all_ok"] is True
    assert len(client.saves()) == 1
    saved = client.saves()[0]
    assert saved["views"][0]["sections"][0]["cards"][2]["entity"] == "light.hall"
    assert saved["views"][1]["path"] == "k"
    backup = Path(data["backup"])
    assert backup.exists()  # noqa: ASYNC240 - test assertion, not production I/O
    assert json.loads(backup.read_text()) == _dashboard()  # noqa: ASYNC240
    assert store.get(plan_id) is None


async def test_refuses_unapproved_plan(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    ctx = _apply_ctx(tmp_path, client, store, plan_id, approved_plan_ids=frozenset())
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, ctx)
    assert result.error_code == "plan_not_approved"
    assert client.saves() == []
    assert store.get(plan_id) is not None


async def test_requires_tier2_approval_flag(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    ctx = _apply_ctx(tmp_path, client, store, plan_id, user_approved=False)
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, ctx)
    assert result.error_code == "confirmation_required"


async def test_unknown_or_expired_plan(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    ctx = _apply_ctx(tmp_path, client, store, "zzzz")
    result = await execute("apply_dashboard_plan", {"plan_id": "zzzz"}, ctx)
    assert result.error_code == "plan_not_found"


async def test_other_conversation_plan_is_not_found(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    ctx = _apply_ctx(tmp_path, client, store, plan_id, conversation_id="someone-else")
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, ctx)
    assert result.error_code == "plan_not_found"


async def test_target_changed_aborts_before_save(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(
        tmp_path,
        client,
        store,
        [{"op": "remove_card", "view_path": "rooms", "section": "Lights", "card_index": 1}],
    )
    # Someone swapped the card between plan and apply.
    client.config["views"][0]["sections"][0]["cards"][1] = {"type": "tile", "entity": "light.hall"}
    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.error_code == "target_changed"
    assert result.data["op_index"] == 0
    assert result.data["actual"]["entity"] == "light.hall"
    assert client.saves() == []


async def test_save_failure_reports_ha_error(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard(), save_raises=CommandError("home_assistant_error", "boom"))
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.error_code == "ha_error"
    assert result.data["backup"]


async def test_readback_mismatch_reported(tmp_path: Path) -> None:
    def _drop_new_card(saved: dict[str, Any]) -> dict[str, Any]:
        saved["views"][0]["sections"][0]["cards"].pop()
        return saved

    client = _FakeClient(_dashboard(), tamper=_drop_new_card)
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS[:1])
    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.status.value == "ok"
    assert result.data["verification"]["all_ok"] is False
    assert result.data["verification"]["ops"][0]["ok"] is False
