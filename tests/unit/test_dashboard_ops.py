"""Pure dashboard op functions: build, resolve, apply, receipts."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from mylo.dashboard.ops import (
    OpError,
    TargetMismatch,
    apply_op,
    build_section,
    build_view,
    find_view_index,
    resolve_position,
    resolve_section,
)
from mylo.dashboard.plan import (
    AddCards,
    AddSection,
    CardFingerprint,
    CreateView,
    DeleteView,
    MoveCard,
    PlanSection,
    RemoveCard,
    RemoveSection,
    ReplaceCard,
    ResolvedTarget,
    UpdateViewMeta,
)


def _config() -> dict[str, Any]:
    return {
        "views": [
            {"path": "home", "title": "Home", "cards": [{"type": "tile", "entity": "light.a"}]},
            {
                "path": "rooms",
                "title": "Rooms",
                "type": "sections",
                "max_columns": 4,
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Lights"},
                            {"type": "tile", "entity": "light.kitchen"},
                            {"type": "tile", "entity": "light.hall"},
                        ],
                    },
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Climate"},
                            {"type": "thermostat", "entity": "climate.main"},
                        ],
                    },
                ],
            },
        ]
    }


def _rooms(config: dict[str, Any]) -> dict[str, Any]:
    return config["views"][find_view_index(config, "rooms") or 0]


# ─── helpers ────────────────────────────────────────────────────────────────


def test_build_section_prepends_heading_once() -> None:
    sec = build_section(PlanSection(heading="Lights", cards=[{"type": "tile", "entity": "l.a"}]))
    assert sec == {
        "type": "grid",
        "cards": [{"type": "heading", "heading": "Lights"}, {"type": "tile", "entity": "l.a"}],
    }
    sec = build_section(
        PlanSection(
            heading="Lights",
            cards=[{"type": "heading", "heading": "Old"}, {"type": "tile", "entity": "l.a"}],
            column_span=2,
        )
    )
    assert sec["cards"][0] == {"type": "heading", "heading": "Lights"}
    assert len(sec["cards"]) == 2
    assert sec["column_span"] == 2


def test_build_view_sections_and_masonry() -> None:
    v = build_view(
        CreateView(
            op="create_view",
            title="K",
            path="k",
            icon="mdi:x",
            sections=[PlanSection(heading="A", cards=[])],
        )
    )
    assert v["type"] == "sections"
    assert v["max_columns"] == 4
    assert "cards" not in v
    assert v["sections"][0]["cards"][0]["heading"] == "A"
    m = build_view(
        CreateView(
            op="create_view", title="K", path="k", layout="masonry", cards=[{"type": "tile"}]
        )
    )
    assert "sections" not in m
    assert m["cards"] == [{"type": "tile"}]


def test_resolve_section_by_index_and_heading() -> None:
    view = _rooms(_config())
    assert resolve_section(view, 1) == 1
    assert resolve_section(view, "climate") == 1
    with pytest.raises(OpError) as exc:
        resolve_section(view, 5)
    assert exc.value.code == "section_index_out_of_range"
    with pytest.raises(OpError) as exc:
        resolve_section(view, "Garage")
    assert exc.value.code == "section_not_found"
    with pytest.raises(OpError) as exc:
        resolve_section(view, None)
    assert exc.value.code == "section_required"


def test_resolve_section_ambiguous_heading() -> None:
    view = _rooms(_config())
    view["sections"].append(copy.deepcopy(view["sections"][0]))
    with pytest.raises(OpError) as exc:
        resolve_section(view, "Lights")
    assert exc.value.code == "section_ambiguous"


def test_resolve_section_on_masonry() -> None:
    view = _config()["views"][0]
    assert resolve_section(view, None) is None
    with pytest.raises(OpError) as exc:
        resolve_section(view, 0)
    assert exc.value.code == "section_not_applicable"


def test_resolve_position() -> None:
    assert resolve_position("end", 3) == 3
    assert resolve_position("start", 3) == 0
    assert resolve_position("start", 3, heading_first=True) == 1
    assert resolve_position("start", 0, heading_first=True) == 0
    assert resolve_position(2, 3) == 2
    assert resolve_position(3, 3) == 3
    with pytest.raises(OpError) as exc:
        resolve_position(4, 3)
    assert exc.value.code == "position_out_of_range"
    with pytest.raises(OpError):
        resolve_position(-1, 3)


# ─── apply_op ───────────────────────────────────────────────────────────────


def test_apply_does_not_mutate_input() -> None:
    config = _config()
    snapshot = copy.deepcopy(config)
    apply_op(config, DeleteView(op="delete_view", view_path="home"), ResolvedTarget(op_index=0))
    assert config == snapshot


def test_create_view_at_position() -> None:
    op = CreateView(op="create_view", title="K", path="k", position="start")
    out, receipt = apply_op(_config(), op, ResolvedTarget(op_index=0))
    assert out["views"][0]["path"] == "k"
    assert receipt.view_index == 0
    assert receipt.view_path == "k"
    with pytest.raises(OpError) as exc:
        apply_op(out, op, ResolvedTarget(op_index=1))
    assert exc.value.code == "view_exists"


def test_add_section_at_index() -> None:
    op = AddSection(
        op="add_section", view_path="rooms", section=PlanSection(heading="Media"), position=1
    )
    out, receipt = apply_op(_config(), op, ResolvedTarget(op_index=0, view_index=1))
    headings = [s["cards"][0]["heading"] for s in _rooms(out)["sections"]]
    assert headings == ["Lights", "Media", "Climate"]
    assert receipt.section_index == 1


def test_add_cards_start_keeps_heading_first() -> None:
    op = AddCards(
        op="add_cards",
        view_path="rooms",
        section="Lights",
        cards=[{"type": "tile", "entity": "light.new"}],
        position="start",
    )
    out, receipt = apply_op(
        _config(), op, ResolvedTarget(op_index=0, view_index=1, section_index=0)
    )
    cards = _rooms(out)["sections"][0]["cards"]
    assert cards[0]["type"] == "heading"
    assert cards[1]["entity"] == "light.new"
    assert receipt.card_indices == [1]


def test_add_cards_masonry_appends() -> None:
    op = AddCards(op="add_cards", view_path="home", cards=[{"type": "tile", "entity": "l.b"}])
    out, receipt = apply_op(_config(), op, ResolvedTarget(op_index=0, view_index=0))
    assert [c["entity"] for c in out["views"][0]["cards"]] == ["light.a", "l.b"]
    assert receipt.card_indices == [1]


def test_replace_card_checks_fingerprint() -> None:
    op = ReplaceCard(
        op="replace_card",
        view_path="rooms",
        section=0,
        card_index=1,
        card={"type": "light", "entity": "light.kitchen"},
    )
    good = ResolvedTarget(
        op_index=0,
        view_index=1,
        section_index=0,
        card_index=1,
        fingerprint=CardFingerprint(type="tile", entity="light.kitchen"),
    )
    out, _ = apply_op(_config(), op, good)
    assert _rooms(out)["sections"][0]["cards"][1]["type"] == "light"
    bad = good.model_copy(
        update={"fingerprint": CardFingerprint(type="tile", entity="light.other")}
    )
    with pytest.raises(TargetMismatch) as exc:
        apply_op(_config(), op, bad)
    assert exc.value.code == "target_changed"
    assert exc.value.actual.entity == "light.kitchen"


def test_remove_card_receipt_has_expected_count() -> None:
    op = RemoveCard(op="remove_card", view_path="rooms", section=0, card_index=2)
    out, receipt = apply_op(
        _config(),
        op,
        ResolvedTarget(
            op_index=0,
            view_index=1,
            section_index=0,
            card_index=2,
            fingerprint=CardFingerprint(type="tile", entity="light.hall"),
        ),
    )
    assert len(_rooms(out)["sections"][0]["cards"]) == 2
    assert receipt.expected_count == 2


def test_move_card_between_sections() -> None:
    op = MoveCard(
        op="move_card",
        view_path="rooms",
        from_section="Lights",
        card_index=2,
        to_section="Climate",
        position="start",
    )
    resolved = ResolvedTarget(
        op_index=0,
        view_index=1,
        section_index=0,
        to_section_index=1,
        card_index=2,
        fingerprint=CardFingerprint(type="tile", entity="light.hall"),
    )
    out, receipt = apply_op(_config(), op, resolved)
    climate = _rooms(out)["sections"][1]["cards"]
    assert [c.get("entity") for c in climate] == [None, "light.hall", "climate.main"]
    assert len(_rooms(out)["sections"][0]["cards"]) == 2
    assert receipt.section_index == 1
    assert receipt.card_indices == [1]


def test_move_card_within_section_to_end() -> None:
    op = MoveCard(op="move_card", view_path="rooms", from_section=0, card_index=1, position="end")
    resolved = ResolvedTarget(
        op_index=0,
        view_index=1,
        section_index=0,
        to_section_index=0,
        card_index=1,
        fingerprint=CardFingerprint(type="tile", entity="light.kitchen"),
    )
    out, receipt = apply_op(_config(), op, resolved)
    lights = _rooms(out)["sections"][0]["cards"]
    assert [c.get("entity") for c in lights] == [None, "light.hall", "light.kitchen"]
    assert receipt.card_indices == [2]


def test_update_view_meta_and_new_path() -> None:
    op = UpdateViewMeta(
        op="update_view_meta", view_path="rooms", title="Rooms 2", max_columns=3, new_path="rooms2"
    )
    out, receipt = apply_op(_config(), op, ResolvedTarget(op_index=0, view_index=1))
    view = out["views"][1]
    assert view["title"] == "Rooms 2"
    assert view["max_columns"] == 3
    assert view["path"] == "rooms2"
    assert receipt.view_path == "rooms2"
    clash = UpdateViewMeta(op="update_view_meta", view_path="rooms2", new_path="home")
    with pytest.raises(OpError) as exc:
        apply_op(out, clash, ResolvedTarget(op_index=1, view_index=1))
    assert exc.value.code == "view_exists"


def test_remove_section_and_delete_view() -> None:
    out, receipt = apply_op(
        _config(),
        RemoveSection(op="remove_section", view_path="rooms", section=0),
        ResolvedTarget(op_index=0, view_index=1, section_index=0),
    )
    assert len(_rooms(out)["sections"]) == 1
    assert receipt.expected_count == 1
    out, _ = apply_op(
        out, DeleteView(op="delete_view", view_path="home"), ResolvedTarget(op_index=1)
    )
    assert find_view_index(out, "home") is None
    with pytest.raises(OpError) as exc:
        apply_op(out, DeleteView(op="delete_view", view_path="home"), ResolvedTarget(op_index=2))
    assert exc.value.code == "view_not_found"
