"""stage_custom_card: contract → store → preview envelope."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.dashboard.cards import REFERENCE_CARD_SOURCE, CardStore
from mylo.ha.registries import Registries
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


def _ctx(tmp_path: Path, store: CardStore | None = None):
    return make_ctx(
        ws_client=None, registries=Registries(), tmp_path=tmp_path, cards=store or CardStore()
    )


def _params(**over: Any) -> dict[str, Any]:
    base = {
        "element": "mylo-entity-row",
        "description": "Compact row",
        "source": REFERENCE_CARD_SOURCE,
    }
    base.update(over)
    return base


async def test_valid_card_is_staged(tmp_path: Path) -> None:
    store = CardStore()
    result = await execute(
        "stage_custom_card",
        _params(config_example={"type": "custom:mylo-entity-row", "entity": "light.a"}),
        _ctx(tmp_path, store),
    )
    assert result.status.value == "ok", result.error_message
    d = result.data
    assert d["preview"] is True and d["action"] == "create" and d["previous_source"] is None
    assert d["url"].startswith("/local/mylo-cards/mylo-entity-row.js?v=")
    assert d["line_count"] > 10 and d["warnings"] == []
    staged = store.get(d["card_id"])
    assert staged is not None and staged.path.endswith("www/mylo-cards/mylo-entity-row.js")


async def test_invalid_card_not_staged(tmp_path: Path) -> None:
    store = CardStore()
    result = await execute(
        "stage_custom_card", _params(source="class A extends Thing {}"), _ctx(tmp_path, store)
    )
    assert result.error_code == "card_invalid"
    assert any(i["code"] == "defines_element" for i in result.data["issues"])
    assert store.staged("test") == []


async def test_update_reports_previous_source(tmp_path: Path) -> None:
    path = tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js"
    path.parent.mkdir(parents=True)
    old = REFERENCE_CARD_SOURCE.replace("Compact tappable", "Old")
    path.write_text(old)
    result = await execute("stage_custom_card", _params(), _ctx(tmp_path))
    assert result.data["action"] == "update" and result.data["previous_source"] == old


async def test_unchanged_source_rejected(tmp_path: Path) -> None:
    path = tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js"
    path.parent.mkdir(parents=True)
    path.write_text(REFERENCE_CARD_SOURCE)
    result = await execute("stage_custom_card", _params(), _ctx(tmp_path))
    assert result.error_code == "card_unchanged"


async def test_two_calls_distinct_ids(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = await execute("stage_custom_card", _params(), ctx)
    b = await execute("stage_custom_card", _params(), ctx)
    assert a.data["card_id"] != b.data["card_id"]


async def test_bad_element_name(tmp_path: Path) -> None:
    result = await execute("stage_custom_card", _params(element="game-row"), _ctx(tmp_path))
    assert result.error_code == "card_invalid"
    assert any(i["code"] == "element_name" for i in result.data["issues"])
