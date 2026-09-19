"""validate_plan: target resolution, refs, schema, theme, lint."""

from __future__ import annotations

from typing import Any

from mylo.dashboard.plan import PlanDashboardParams
from mylo.dashboard.validate import validate_plan
from mylo.ha.registries import EntityEntry, Registries


def _registries(*entity_ids: str) -> Registries:
    reg = Registries()
    reg.entities = {
        e: EntityEntry.from_raw({"entity_id": e, "original_name": e}) for e in entity_ids
    }
    return reg


REG = _registries("light.kitchen", "light.hall", "climate.main", "sensor.temp")


def _config() -> dict[str, Any]:
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


def _params(*ops: dict[str, Any], **extra: Any) -> PlanDashboardParams:
    return PlanDashboardParams.model_validate({"summary": "s", "operations": list(ops), **extra})


def _run(params: PlanDashboardParams, config: dict[str, Any] | None = None, **kw: Any):
    kw.setdefault("registries", REG)
    kw.setdefault("installed_custom", None)
    kw.setdefault("theme_names", None)
    return validate_plan(params, config if config is not None else _config(), **kw)


def _codes(v) -> list[str]:
    return [i.code for i in v.issues]


def test_valid_create_view_resolves_and_applies() -> None:
    v = _run(
        _params(
            {
                "op": "create_view",
                "title": "K",
                "path": "k",
                "sections": [{"heading": "A", "cards": [{"type": "tile", "entity": "light.hall"}]}],
            },
            {
                "op": "add_cards",
                "view_path": "k",
                "section": "A",
                "cards": [{"type": "tile", "entity": "sensor.temp"}],
            },
        )
    )
    assert v.ok, v.issues
    assert len(v.resolved) == 2
    assert v.resolved[1].section_index == 0
    assert v.entity_refs_checked == 2
    assert [vw["path"] for vw in v.result_config["views"]] == ["rooms", "k"]


def test_view_not_found_and_stops_at_first_error() -> None:
    v = _run(
        _params(
            {
                "op": "add_cards",
                "view_path": "nope",
                "cards": [{"type": "tile", "entity": "light.hall"}],
            },
            {"op": "delete_view", "view_path": "rooms"},
        )
    )
    assert not v.ok
    assert _codes(v) == ["view_not_found"]
    assert v.issues[0].op_index == 0
    assert len(v.resolved) == 0


def test_view_exists_on_create() -> None:
    v = _run(_params({"op": "create_view", "title": "R", "path": "rooms"}))
    assert _codes(v) == ["view_exists"]


def test_section_errors() -> None:
    assert _codes(
        _run(
            _params(
                {
                    "op": "add_cards",
                    "view_path": "rooms",
                    "cards": [{"type": "tile", "entity": "light.hall"}],
                }
            )
        )
    ) == ["section_required"]
    assert _codes(
        _run(
            _params(
                {
                    "op": "add_cards",
                    "view_path": "rooms",
                    "section": "Garage",
                    "cards": [{"type": "tile", "entity": "light.hall"}],
                }
            )
        )
    ) == ["section_not_found"]
    assert _codes(
        _run(
            _params(
                {
                    "op": "add_cards",
                    "view_path": "rooms",
                    "section": 3,
                    "cards": [{"type": "tile", "entity": "light.hall"}],
                }
            )
        )
    ) == ["section_index_out_of_range"]


def test_card_index_out_of_range_and_fingerprint_recorded() -> None:
    v = _run(_params({"op": "remove_card", "view_path": "rooms", "section": 0, "card_index": 9}))
    assert _codes(v) == ["card_index_out_of_range"]
    v = _run(
        _params({"op": "remove_card", "view_path": "rooms", "section": "Lights", "card_index": 1})
    )
    assert v.ok
    fp = v.resolved[0].fingerprint
    assert fp is not None and fp.type == "tile" and fp.entity == "light.kitchen"
    assert v.resolved[0].section_heading == "Lights"


def test_move_card_resolves_both_sections() -> None:
    config = _config()
    config["views"][0]["sections"].append(
        {"type": "grid", "cards": [{"type": "heading", "heading": "Climate"}]}
    )
    v = _run(
        _params(
            {
                "op": "move_card",
                "view_path": "rooms",
                "from_section": "Lights",
                "card_index": 1,
                "to_section": "Climate",
                "position": "start",
            }
        ),
        config,
    )
    assert v.ok, v.issues
    assert v.resolved[0].section_index == 0
    assert v.resolved[0].to_section_index == 1
    assert v.resolved[0].to_section_heading == "Climate"
    assert v.result_config["views"][0]["sections"][1]["cards"][1]["entity"] == "light.kitchen"


def test_position_out_of_range() -> None:
    v = _run(
        _params(
            {"op": "add_section", "view_path": "rooms", "section": {"heading": "X"}, "position": 5}
        )
    )
    assert _codes(v) == ["position_out_of_range"]


def test_invalid_entity_refs_with_suggestion() -> None:
    v = _run(
        _params(
            {
                "op": "add_cards",
                "view_path": "rooms",
                "section": 0,
                "cards": [{"type": "tile", "entity": "light.kitchn"}],
            }
        )
    )
    assert _codes(v) == ["invalid_entity_refs"]
    assert "light.kitchen" in v.issues[0].message
    assert v.issues[0].op_index == 0


def test_card_schema_errors_block() -> None:
    v = _run(
        _params(
            {"op": "add_cards", "view_path": "rooms", "section": 0, "cards": [{"type": "tile"}]}
        )
    )
    assert "card_schema" in _codes(v)
    assert not v.ok


def test_custom_card_not_installed_blocks_when_resources_known() -> None:
    v = _run(
        _params(
            {
                "op": "add_cards",
                "view_path": "rooms",
                "section": 0,
                "cards": [{"type": "custom:nope-card", "entity": "light.hall"}],
            }
        ),
        installed_custom={"custom:mushroom-light-card"},
    )
    assert not v.ok
    v = _run(
        _params(
            {
                "op": "add_cards",
                "view_path": "rooms",
                "section": 0,
                "cards": [{"type": "custom:nope-card", "entity": "light.hall"}],
            }
        ),
        installed_custom=None,
    )
    assert v.ok and any(i.severity == "warning" for i in v.issues)


def test_theme_not_installed() -> None:
    v = _run(
        _params({"op": "create_view", "title": "K", "path": "k", "theme": "noctis"}),
        theme_names=["ios"],
    )
    assert _codes(v) == ["theme_not_installed"]
    v = _run(
        _params({"op": "create_view", "title": "K", "path": "k", "theme": "noctis"}),
        theme_names=None,
    )
    assert v.ok


def test_lint_warnings() -> None:
    v = _run(
        _params(
            {
                "op": "create_view",
                "title": "K",
                "path": "k",
                "sections": [
                    {"heading": "Empty", "cards": []},
                    {"heading": "Nothing", "cards": []},
                    {"heading": "Big", "cards": [{"type": "tile", "entity": "light.hall"}] * 11},
                    {
                        "heading": "Dup",
                        "cards": [
                            {"type": "tile", "entity": "sensor.temp"},
                            {"type": "tile", "entity": "sensor.temp"},
                        ],
                    },
                ],
            },
            {
                "op": "add_cards",
                "view_path": "k",
                "section": "Empty",
                "cards": [{"type": "tile", "entity": "climate.main"}],
                "position": 0,
            },
            {"op": "create_view", "title": "M", "path": "m", "layout": "masonry"},
        )
    )
    assert v.ok
    codes = set(_codes(v))
    assert {
        "lint_empty_section",
        "lint_section_too_large",
        "lint_duplicate_entity",
        "lint_no_heading",
        "lint_masonry_view",
    } <= codes
    assert all(i.severity == "warning" for i in v.issues)


def test_independent_stages_all_report() -> None:
    """A bad entity ref (stage 2), a card missing its option (stage 3),
    and a missing theme (stage 4) are all reported in one pass."""
    v = _run(
        _params(
            {
                "op": "create_view",
                "title": "K",
                "path": "k",
                "theme": "nope",
                "sections": [
                    {
                        "heading": "A",
                        "cards": [{"type": "tile", "entity": "light.kitchn"}, {"type": "gauge"}],
                    }
                ],
            },
        ),
        theme_names=["ios"],
    )
    assert not v.ok
    codes = _codes(v)
    assert "invalid_entity_refs" in codes
    assert "card_schema" in codes
    assert "theme_not_installed" in codes
    assert not any(c.startswith("lint_") for c in codes)


def test_add_section_on_masonry_view_not_applicable() -> None:
    config = {"views": [{"path": "home", "title": "Home", "cards": []}]}
    v = _run(
        _params({"op": "add_section", "view_path": "home", "section": {"heading": "X"}}), config
    )
    assert _codes(v) == ["section_not_applicable"]


def test_section_heading_ambiguous() -> None:
    config = _config()
    config["views"][0]["sections"].append(
        {"type": "grid", "cards": [{"type": "heading", "heading": "Lights"}]}
    )
    v = _run(
        _params(
            {
                "op": "add_cards",
                "view_path": "rooms",
                "section": "Lights",
                "cards": [{"type": "tile", "entity": "light.hall"}],
            }
        ),
        config,
    )
    assert _codes(v) == ["section_ambiguous"]
