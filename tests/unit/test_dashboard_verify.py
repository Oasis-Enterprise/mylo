from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from mylo.dashboard.ops import apply_op
from mylo.dashboard.plan import DashboardPlan, PlanDashboardParams
from mylo.dashboard.validate import validate_plan
from mylo.dashboard.verify import verify_plan
from mylo.ha.registries import EntityEntry, Registries


def _reg() -> Registries:
    reg = Registries()
    reg.entities = {
        e: EntityEntry.from_raw({"entity_id": e}) for e in ("light.a", "light.b", "climate.c")
    }
    return reg


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
                            {"type": "tile", "entity": "light.a"},
                            {"type": "tile", "entity": "light.b"},
                        ],
                    },
                    {"type": "grid", "cards": [{"type": "heading", "heading": "Climate"}]},
                ],
            },
            {"path": "old", "title": "Old", "cards": []},
        ]
    }


def _plan_and_apply(ops: list[dict[str, Any]]) -> tuple[DashboardPlan, list[Any], dict[str, Any]]:
    params = PlanDashboardParams(summary="s", operations=ops)  # type: ignore[arg-type]
    v = validate_plan(params, _config(), registries=_reg(), installed_custom=None, theme_names=None)
    assert v.ok, v.issues
    plan = DashboardPlan(
        plan_id="p1",
        dashboard_id=None,
        summary="s",
        assumptions=[],
        operations=params.operations,
        resolved=v.resolved,
        issues=v.issues,
        created_at=datetime.now(UTC),
        conversation_id="c",
    )
    work = _config()
    receipts = []
    for op, target in zip(plan.operations, plan.resolved, strict=True):
        work, r = apply_op(work, op, target)
        receipts.append(r)
    return plan, receipts, work


ALL_OPS = [
    {
        "op": "create_view",
        "title": "K",
        "path": "k",
        "position": "start",
        "sections": [{"heading": "A", "cards": [{"type": "tile", "entity": "light.a"}]}],
    },
    {
        "op": "add_section",
        "view_path": "rooms",
        "section": {"heading": "Media", "cards": [{"type": "tile", "entity": "light.b"}]},
        "position": 1,
    },
    {
        "op": "add_cards",
        "view_path": "rooms",
        "section": "Climate",
        "cards": [{"type": "thermostat", "entity": "climate.c"}],
    },
    {
        "op": "move_card",
        "view_path": "rooms",
        "from_section": "Lights",
        "card_index": 2,
        "to_section": "Climate",
        "position": "start",
    },
    {
        "op": "replace_card",
        "view_path": "rooms",
        "section": "Lights",
        "card_index": 1,
        "card": {"type": "light", "entity": "light.a"},
    },
    {"op": "remove_card", "view_path": "rooms", "section": "Media", "card_index": 1},
    {"op": "update_view_meta", "view_path": "rooms", "title": "Rooms!", "new_path": "rooms2"},
    {"op": "remove_section", "view_path": "rooms2", "section": "Media"},
    {"op": "delete_view", "view_path": "old"},
]


def test_faithful_readback_verifies_every_op() -> None:
    plan, receipts, after = _plan_and_apply(ALL_OPS)
    result = verify_plan(plan, receipts, after, after)
    assert result["all_ok"], result
    assert [r["op"] for r in result["ops"]] == [o["op"] for o in ALL_OPS]
    assert all(r["ok"] for r in result["ops"])


def test_dropped_card_is_detected() -> None:
    plan, receipts, after = _plan_and_apply(ALL_OPS[:3])
    tampered = copy.deepcopy(after)
    tampered["views"][1]["sections"][2]["cards"].pop()  # the added thermostat
    result = verify_plan(plan, receipts, after, tampered)
    assert not result["all_ok"]
    assert "differs" in result["reason"]
    assert result["ops"][2]["ok"] is False
    assert "thermostat" in result["ops"][2]["detail"]


def test_missing_view_is_detected() -> None:
    plan, receipts, after = _plan_and_apply(ALL_OPS[:1])
    result = verify_plan(plan, receipts, after, {"views": []})
    assert not result["all_ok"]
    assert "not found" in result["ops"][0]["detail"]
