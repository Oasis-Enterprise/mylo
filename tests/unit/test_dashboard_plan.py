"""Plan model: discriminated ops, path slugs, positions, fingerprints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mylo.dashboard.plan import (
    AddCards,
    CreateView,
    MoveCard,
    PlanDashboardParams,
    card_fingerprint,
    is_heading_card,
)


def test_ops_discriminate_on_op_field() -> None:
    params = PlanDashboardParams.model_validate(
        {
            "summary": "Kitchen view",
            "operations": [
                {
                    "op": "create_view",
                    "title": "Kitchen",
                    "path": "kitchen",
                    "sections": [
                        {"heading": "Lights", "cards": [{"type": "tile", "entity": "light.a"}]}
                    ],
                },
                {
                    "op": "add_cards",
                    "view_path": "kitchen",
                    "section": "Lights",
                    "cards": [{"type": "tile"}],
                },
                {
                    "op": "move_card",
                    "view_path": "kitchen",
                    "from_section": 0,
                    "card_index": 1,
                    "position": "start",
                },
            ],
        }
    )
    assert isinstance(params.operations[0], CreateView)
    assert isinstance(params.operations[1], AddCards)
    assert isinstance(params.operations[2], MoveCard)
    assert params.operations[0].position == "end"
    assert params.operations[2].position == "start"
    assert params.operations[2].to_section is None


def test_create_view_requires_slug_path() -> None:
    with pytest.raises(ValidationError):
        CreateView(op="create_view", title="Kitchen", path="Kitchen Stuff")
    with pytest.raises(ValidationError):
        CreateView(op="create_view", title="Kitchen", path="")


def test_position_accepts_int_or_keyword() -> None:
    op = AddCards(op="add_cards", view_path="k", cards=[{"type": "tile"}], position=2)
    assert op.position == 2
    op = AddCards(op="add_cards", view_path="k", cards=[{"type": "tile"}], position="start")
    assert op.position == "start"
    with pytest.raises(ValidationError):
        AddCards(op="add_cards", view_path="k", cards=[{"type": "tile"}], position="middle")


def test_unknown_op_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanDashboardParams.model_validate(
            {"summary": "x", "operations": [{"op": "update", "config": {}}]}
        )


def test_operations_bounded() -> None:
    with pytest.raises(ValidationError):
        PlanDashboardParams.model_validate({"summary": "x", "operations": []})
    too_many = [{"op": "delete_view", "view_path": f"v{i}"} for i in range(41)]
    with pytest.raises(ValidationError):
        PlanDashboardParams.model_validate({"summary": "x", "operations": too_many})


def test_card_fingerprint_uses_entity_or_first_of_entities() -> None:
    assert card_fingerprint({"type": "tile", "entity": "light.a"}).model_dump() == {
        "type": "tile",
        "entity": "light.a",
    }
    fp = card_fingerprint({"type": "entities", "entities": [{"entity": "sensor.t"}, "sensor.h"]})
    assert fp.entity == "sensor.t"
    fp = card_fingerprint({"type": "history-graph", "entities": ["sensor.h"]})
    assert fp.entity == "sensor.h"
    assert card_fingerprint({"type": "markdown", "content": "hi"}).entity is None
    assert card_fingerprint("not a card").type == "?"


def test_is_heading_card() -> None:
    assert is_heading_card({"type": "heading", "heading": "Lights"})
    assert not is_heading_card({"type": "tile"})
    assert not is_heading_card(None)
