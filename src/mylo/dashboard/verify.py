# Copyright 2026 Maxwell Monson / Oasis Enterprise LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Post-apply verification.

The primary check is exact equality between what the executor
computed and what HA read back — if they match, every op landed by
construction. Only on a mismatch do the per-op predicates run, to
name the op whose target looks wrong. (Ops inside one plan can
supersede each other, so per-op predicates against the final state
are diagnostics, not the verdict.)
"""

from __future__ import annotations

from typing import Any

from mylo.dashboard.ops import (
    OpReceipt,
    build_section,
    cards_at,
    find_view_index,
    section_heading,
)
from mylo.dashboard.plan import (
    AddCards,
    AddSection,
    CreateView,
    DashboardPlan,
    DeleteView,
    MoveCard,
    PlanOp,
    RemoveCard,
    RemoveSection,
    ReplaceCard,
    UpdateViewMeta,
    card_fingerprint,
)


def verify_plan(
    plan: DashboardPlan,
    receipts: list[OpReceipt],
    expected_config: dict[str, Any],
    config_after: dict[str, Any],
) -> dict[str, Any]:
    if config_after == expected_config:
        return {
            "all_ok": True,
            "ops": [
                {"op_index": r.op_index, "op": r.op, "ok": True, "detail": r.detail}
                for r in receipts
            ],
        }
    results: list[dict[str, Any]] = []
    for op, receipt in zip(plan.operations, receipts, strict=True):
        ok, detail = _verify_one(plan, op, receipt, config_after)
        results.append({"op_index": receipt.op_index, "op": receipt.op, "ok": ok, "detail": detail})
    return {
        "all_ok": False,
        "reason": "read-back config differs from the computed config",
        "ops": results,
    }


def _verify_one(
    plan: DashboardPlan, op: PlanOp, receipt: OpReceipt, config: dict[str, Any]
) -> tuple[bool, str]:
    views = config.get("views") or []

    if isinstance(op, DeleteView):
        if find_view_index(config, op.view_path) is None:
            return True, f"view {op.view_path!r} absent"
        return False, f"view {op.view_path!r} still present"

    path = receipt.view_path or ""
    vi = find_view_index(config, path)
    if vi is None:
        return False, f"view {path!r} not found after save"
    view = views[vi]

    if isinstance(op, CreateView):
        if vi != receipt.view_index:
            return False, f"view {path!r} at index {vi}, expected {receipt.view_index}"
        if op.layout == "masonry":
            n = len(view.get("cards") or [])
            if n != len(op.cards):
                return False, f"view {path!r} has {n} cards, expected {len(op.cards)}"
            return True, f"view {path!r} at index {vi}, {n} cards"
        sections = view.get("sections") or []
        if len(sections) != len(op.sections):
            return False, f"view {path!r} has {len(sections)} sections, expected {len(op.sections)}"
        for si, (section, planned) in enumerate(zip(sections, op.sections, strict=True)):
            if section_heading(section) != planned.heading:
                return (
                    False,
                    f"section {si} heading is {section_heading(section)!r}, expected {planned.heading!r}",
                )
            expected = len(build_section(planned)["cards"])
            actual = len(section.get("cards") or [])
            if actual != expected:
                return False, f"section {si} has {actual} cards, expected {expected}"
        return True, f"view {path!r} at index {vi}, {len(sections)} sections"

    if isinstance(op, AddSection):
        sections = view.get("sections") or []
        section_index = receipt.section_index
        if section_index is None or not 0 <= section_index < len(sections):
            return False, f"section index {section_index} missing"
        if section_heading(sections[section_index]) != op.section.heading:
            return (
                False,
                f"section {section_index} heading is "
                f"{section_heading(sections[section_index])!r}, expected {op.section.heading!r}",
            )
        expected = len(build_section(op.section)["cards"])
        actual = len(sections[section_index].get("cards") or [])
        if actual != expected:
            return False, f"section {section_index} has {actual} cards, expected {expected}"
        return True, f"section {op.section.heading!r} at index {section_index}, {actual} cards"

    if isinstance(op, AddCards):
        cards = cards_at(view, receipt.section_index)
        for idx, planned_card in zip(receipt.card_indices, op.cards, strict=True):
            want = card_fingerprint(planned_card)
            if idx >= len(cards) or card_fingerprint(cards[idx]) != want:
                return (
                    False,
                    f"card at index {idx} is not the planned {want.type} ({want.entity or '-'})",
                )
        return True, f"{len(op.cards)} cards at indices {receipt.card_indices}"

    if isinstance(op, ReplaceCard):
        cards = cards_at(view, receipt.section_index)
        want = card_fingerprint(op.card)
        if op.card_index < len(cards) and card_fingerprint(cards[op.card_index]) == want:
            return True, f"card {op.card_index} is now {want.type} ({want.entity or '-'})"
        return False, f"card {op.card_index} is not the replacement {want.type}"

    if isinstance(op, RemoveCard | RemoveSection):
        if isinstance(op, RemoveCard):
            cards = cards_at(view, receipt.section_index)
            actual = len(cards)
            noun = "cards"
        else:
            cards = []
            actual = len(view.get("sections") or [])
            noun = "sections"
        if actual == receipt.expected_count:
            if isinstance(op, RemoveCard):
                fp = plan.resolved[receipt.op_index].fingerprint
                if op.card_index < len(cards) and card_fingerprint(cards[op.card_index]) == fp:
                    return False, "removed card still present at its index"
            return True, f"{actual} {noun} remain"
        return False, f"{actual} {noun} remain, expected {receipt.expected_count}"

    if isinstance(op, MoveCard):
        dst = cards_at(view, receipt.section_index)
        fp = plan.resolved[receipt.op_index].fingerprint
        idx = receipt.card_indices[0] if receipt.card_indices else -1
        if fp is not None and 0 <= idx < len(dst) and card_fingerprint(dst[idx]) == fp:
            return (
                True,
                f"{fp.type} ({fp.entity or '-'}) at section {receipt.section_index} index {idx}",
            )
        return False, f"moved card not at section {receipt.section_index} index {idx}"

    if isinstance(op, UpdateViewMeta):
        for key in ("title", "icon", "theme", "max_columns"):
            value = getattr(op, key)
            if value is not None and view.get(key) != value:
                return False, f"{key} is {view.get(key)!r}, expected {value!r}"
        return True, "view metadata matches"

    return False, f"no verifier for {receipt.op}"
