"""read_custom_card: reads the current source of a Mylo-authored card."""

from __future__ import annotations

from pathlib import Path

import pytest

from mylo.dashboard.cards import REFERENCE_CARD_SOURCE
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


def _ctx(tmp_path: Path):
    return make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)


async def test_reads_existing_card(tmp_path: Path) -> None:
    path = tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js"
    path.parent.mkdir(parents=True)
    path.write_text(REFERENCE_CARD_SOURCE)

    result = await execute("read_custom_card", {"element": "mylo-entity-row"}, _ctx(tmp_path))

    assert result.status.value == "ok", result.error_message
    d = result.data
    assert d["element"] == "mylo-entity-row"
    assert d["source"] == REFERENCE_CARD_SOURCE
    assert d["line_count"] == len(REFERENCE_CARD_SOURCE.splitlines())
    assert d["byte_count"] == len(REFERENCE_CARD_SOURCE.encode("utf-8"))
    assert d["path"].endswith("www/mylo-cards/mylo-entity-row.js")


async def test_card_not_found(tmp_path: Path) -> None:
    result = await execute("read_custom_card", {"element": "mylo-missing"}, _ctx(tmp_path))
    assert result.error_code == "card_not_found"


async def test_bad_element_name(tmp_path: Path) -> None:
    result = await execute("read_custom_card", {"element": "game-row"}, _ctx(tmp_path))
    assert result.error_code == "bad_element"
