"""apply_custom_card: approval binding, backup+write, resource registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.dashboard.cards import REFERENCE_CARD_SOURCE, CardStore
from mylo.ha.registries import Registries
from mylo.ha.ws_client import CommandError
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _Client:
    def __init__(
        self,
        resources: list[dict[str, Any]] | None = None,
        *,
        create_raises: Exception | None = None,
    ) -> None:
        self.resources = resources if resources is not None else []
        self.create_raises = create_raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "lovelace/resources":
            return self.resources
        if type_ == "lovelace/resources/create":
            if self.create_raises:
                raise self.create_raises
            return {"id": "new1", "url": kwargs["url"], "type": "module"}
        if type_ == "lovelace/resources/update":
            return {"id": kwargs["resource_id"], "url": kwargs["url"], "type": "module"}
        return {}


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


async def _staged(
    tmp_path: Path, client: _Client, store: CardStore, source: str = REFERENCE_CARD_SOURCE
) -> str:
    ctx = make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path, cards=store)
    r = await execute(
        "stage_custom_card",
        {"element": "mylo-entity-row", "description": "d", "source": source},
        ctx,
    )
    assert r.status.value == "ok", r.data
    return r.data["card_id"]


def _apply_ctx(tmp_path: Path, client: _Client, store: CardStore, card_id: str, **kw: Any):
    return make_ctx(
        ws_client=client,
        registries=Registries(),
        tmp_path=tmp_path,
        cards=store,
        user_approved=kw.pop("user_approved", True),
        approved_plan_ids=kw.pop("approved_plan_ids", frozenset({card_id})),
        **kw,
    )


async def test_apply_writes_file_and_creates_resource(tmp_path: Path) -> None:
    client, store = _Client(), CardStore()
    card_id = await _staged(tmp_path, client, store)
    result = await execute(
        "apply_custom_card", {"card_id": card_id}, _apply_ctx(tmp_path, client, store, card_id)
    )
    assert result.status.value == "ok", result.error_message
    d = result.data
    assert (
        d["preview"] is False and d["resource_action"] == "created" and d["resource_id"] == "new1"
    )
    assert (
        tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js"
    ).read_text() == REFERENCE_CARD_SOURCE
    assert d["backup"] is None
    assert store.get(card_id) is None


async def test_update_takes_backup_and_bumps_resource(tmp_path: Path) -> None:
    path = tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js"
    path.parent.mkdir(parents=True)
    old = REFERENCE_CARD_SOURCE.replace("Compact tappable", "Old")
    path.write_text(old)
    client = _Client(
        [{"id": "r9", "url": "/local/mylo-cards/mylo-entity-row.js?v=old", "type": "module"}]
    )
    store = CardStore()
    card_id = await _staged(tmp_path, client, store)
    result = await execute(
        "apply_custom_card", {"card_id": card_id}, _apply_ctx(tmp_path, client, store, card_id)
    )
    assert result.data["resource_action"] == "updated" and result.data["resource_id"] == "r9"
    assert result.data["backup"] and Path(result.data["backup"]).read_text() == old  # noqa: ASYNC240
    assert path.read_text() == REFERENCE_CARD_SOURCE


async def test_refuses_unapproved(tmp_path: Path) -> None:
    client, store = _Client(), CardStore()
    card_id = await _staged(tmp_path, client, store)
    result = await execute(
        "apply_custom_card",
        {"card_id": card_id},
        _apply_ctx(tmp_path, client, store, card_id, approved_plan_ids=frozenset()),
    )
    assert result.error_code == "card_not_approved"
    assert not (tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js").exists()


async def test_unknown_card(tmp_path: Path) -> None:
    client, store = _Client(), CardStore()
    result = await execute(
        "apply_custom_card", {"card_id": "zzzz"}, _apply_ctx(tmp_path, client, store, "zzzz")
    )
    assert result.error_code == "card_not_found"


async def test_resource_failure_keeps_file(tmp_path: Path) -> None:
    client = _Client(create_raises=CommandError("home_assistant_error", "boom"))
    store = CardStore()
    card_id = await _staged(tmp_path, client, store)
    result = await execute(
        "apply_custom_card", {"card_id": card_id}, _apply_ctx(tmp_path, client, store, card_id)
    )
    assert result.error_code == "resource_failed"
    assert (tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js").exists()
    assert result.data["url"].startswith("/local/mylo-cards/")


async def test_apply_invalidates_env_cache(tmp_path: Path) -> None:
    from mylo.tools import executor as tool_executor

    client, store = _Client(), CardStore()
    card_id = await _staged(tmp_path, client, store)
    ctx = _apply_ctx(tmp_path, client, store, card_id)
    await execute("query_dashboard_env", {}, ctx)
    before = sum(1 for t, _ in client.calls if t == "lovelace/resources")
    await execute("apply_custom_card", {"card_id": card_id}, ctx)
    await execute("query_dashboard_env", {}, ctx)
    after = sum(1 for t, _ in client.calls if t == "lovelace/resources")
    assert after > before + 1  # apply's own list + a fresh env query, not a cache hit
    tool_executor._result_cache.clear()
