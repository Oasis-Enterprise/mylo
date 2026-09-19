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
        self.fetch_raises: Exception | None = None

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "lovelace/config" and self.fetch_raises is not None:
            raise self.fetch_raises
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


async def test_section_renamed_between_plan_and_apply_aborts(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(
        tmp_path,
        client,
        store,
        [{"op": "remove_section", "view_path": "rooms", "section": "Lights"}],
    )
    # The section got renamed between plan and apply.
    client.config["views"][0]["sections"][0]["cards"][0]["heading"] = "Garage"
    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.error_code == "target_changed"
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


async def test_fetch_failure_reports_dashboard_unavailable(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    client.fetch_raises = CommandError("home_assistant_error", "boom")
    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.error_code == "dashboard_unavailable"
    assert client.saves() == []
    assert store.get(plan_id) is not None


async def test_named_dashboard_vanished_reports_not_found(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    ctx = make_ctx(ws_client=client, registries=_registries(), tmp_path=tmp_path, plans=store)
    staged = await execute(
        "plan_dashboard",
        {"summary": "s", "dashboard_id": "tablet", "operations": OPS},
        ctx,
    )
    plan_id = staged.data["plan_id"]
    client.fetch_raises = CommandError("config_not_found", "gone")
    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.error_code == "dashboard_not_found"
    assert client.saves() == []


async def test_backup_failure_does_not_block_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mylo.tools.write import apply_dashboard_plan as module

    def _boom(*args: Any, **kwargs: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(module, "write_backup", _boom)
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.status.value == "ok"
    assert result.data["backup"] is None
    assert len(client.saves()) == 1
    assert result.data["verification"]["all_ok"] is True


async def test_apply_invalidates_query_dashboard_cache(tmp_path: Path) -> None:
    from mylo.tools import executor

    executor._result_cache.clear()

    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)

    q_ctx = make_ctx(ws_client=client, registries=_registries(), tmp_path=tmp_path)
    q_params = {"dashboard_id": None, "view_id": "rooms"}

    def _config_reads() -> int:
        return sum(1 for t, _ in client.calls if t == "lovelace/config")

    # _staged() already issued one lovelace/config read while validating
    # the plan; count from here so the assertions isolate query_dashboard.
    baseline = _config_reads()
    await execute("query_dashboard", q_params, q_ctx)
    await execute("query_dashboard", q_params, q_ctx)  # cached — no extra read
    assert _config_reads() == baseline + 1

    result = await execute(
        "apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id)
    )
    assert result.status.value == "ok", result.error_message

    before = _config_reads()
    await execute("query_dashboard", q_params, q_ctx)
    assert _config_reads() == before + 1
