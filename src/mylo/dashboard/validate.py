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

"""Validate a dashboard plan against the live config.

Runs the stages in spec §4.3, in order. Stage 1 (target resolution)
stops at the first op that fails, because later ops depend on earlier
ones. Stages 2-4 (entity refs, card schema, theme) are independent and
all run, so the model sees every fixable problem at once. Stage 5
(lint) runs only when there are no errors. Target resolution applies
each op to a working copy so later ops can address views and sections
created earlier in the same plan.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, assert_never

from mylo.dashboard.card_schema import validate_view
from mylo.dashboard.ops import (
    OpError,
    apply_op,
    cards_at,
    find_view_index,
    is_sections_view,
    resolve_position,
    resolve_section,
)
from mylo.dashboard.plan import (
    AddCards,
    AddSection,
    CreateView,
    DeleteView,
    MoveCard,
    PlanDashboardParams,
    PlanIssue,
    PlanOp,
    RemoveCard,
    RemoveSection,
    ReplaceCard,
    ResolvedTarget,
    UpdateViewMeta,
    card_fingerprint,
    is_heading_card,
)
from mylo.ha.registries import Registries
from mylo.tools.dashboard_refs import extract_entity_refs, validate_refs

MAX_SECTION_CARDS = 10


@dataclass(slots=True)
class PlanValidation:
    resolved: list[ResolvedTarget] = field(default_factory=list)
    issues: list[PlanIssue] = field(default_factory=list)
    result_config: dict[str, Any] = field(default_factory=dict)
    entity_refs_checked: int = 0

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)


def new_cards_of(op: PlanOp) -> list[dict[str, Any]]:
    """Cards an op introduces (the ones worth validating)."""
    if isinstance(op, CreateView):
        cards = [c for s in op.sections for c in s.cards]
        return cards + list(op.cards)
    if isinstance(op, AddSection):
        return list(op.section.cards)
    if isinstance(op, AddCards):
        return list(op.cards)
    if isinstance(op, ReplaceCard):
        return [op.card]
    return []


def _touched_path(op: PlanOp) -> str | None:
    if isinstance(op, CreateView):
        return op.path
    if isinstance(op, UpdateViewMeta):
        return op.new_path or op.view_path
    if isinstance(op, DeleteView):
        return None
    return op.view_path


def resolve_target(config: dict[str, Any], op: PlanOp, op_index: int) -> ResolvedTarget:
    views = config.get("views") or []
    if isinstance(op, CreateView):
        if find_view_index(config, op.path) is not None:
            raise OpError("view_exists", f"view path {op.path!r} already exists")
        resolve_position(op.position, len(views))
        return ResolvedTarget(op_index=op_index)

    view_index = find_view_index(config, op.view_path)
    if view_index is None:
        available = [v.get("path") for v in views if isinstance(v, dict)]
        raise OpError(
            "view_not_found", f"no view with path {op.view_path!r}; available: {available}"
        )
    view = views[view_index]
    target = ResolvedTarget(op_index=op_index, view_index=view_index)

    if isinstance(op, DeleteView):
        return target
    if isinstance(op, UpdateViewMeta):
        if (
            op.new_path
            and op.new_path != op.view_path
            and find_view_index(config, op.new_path) is not None
        ):
            raise OpError("view_exists", f"view path {op.new_path!r} already exists")
        return target
    if isinstance(op, AddSection):
        if not is_sections_view(view):
            raise OpError("section_not_applicable", "add_section needs a sections-layout view")
        resolve_position(op.position, len(view.get("sections") or []))
        return target
    if isinstance(op, RemoveSection):
        target.section_index = resolve_section(view, op.section)
        return target
    if isinstance(op, AddCards):
        target.section_index = resolve_section(view, op.section)
        cards = cards_at(view, target.section_index)
        resolve_position(
            op.position, len(cards), heading_first=bool(cards) and is_heading_card(cards[0])
        )
        return target
    if isinstance(op, ReplaceCard | RemoveCard):
        target.section_index = resolve_section(view, op.section)
        cards = cards_at(view, target.section_index)
        if not 0 <= op.card_index < len(cards):
            raise OpError(
                "card_index_out_of_range",
                f"card index {op.card_index} out of range (list has {len(cards)} cards)",
            )
        target.card_index = op.card_index
        target.fingerprint = card_fingerprint(cards[op.card_index])
        return target
    if isinstance(op, MoveCard):
        target.section_index = resolve_section(view, op.from_section)
        src = cards_at(view, target.section_index)
        if not 0 <= op.card_index < len(src):
            raise OpError(
                "card_index_out_of_range",
                f"card index {op.card_index} out of range (list has {len(src)} cards)",
            )
        target.card_index = op.card_index
        target.fingerprint = card_fingerprint(src[op.card_index])
        to_ref = op.to_section if op.to_section is not None else op.from_section
        target.to_section_index = resolve_section(view, to_ref)
        dst = cards_at(view, target.to_section_index)
        length = len(dst) - (1 if target.to_section_index == target.section_index else 0)
        resolve_position(
            op.position, max(length, 0), heading_first=bool(dst) and is_heading_card(dst[0])
        )
        return target
    assert_never(op)


def validate_plan(
    params: PlanDashboardParams,
    current_config: dict[str, Any],
    *,
    registries: Registries,
    installed_custom: set[str] | None,
    theme_names: list[str] | None,
) -> PlanValidation:
    out = PlanValidation(result_config=copy.deepcopy(current_config))
    touched: set[str] = set()

    # 1. Targets, applied sequentially so later ops see earlier results.
    for i, op in enumerate(params.operations):
        try:
            target = resolve_target(out.result_config, op, i)
            out.result_config, _receipt = apply_op(out.result_config, op, target)
        except OpError as exc:
            out.issues.append(
                PlanIssue(severity="error", code=exc.code, message=exc.message, op_index=i)
            )
            return out
        out.resolved.append(target)
        path = _touched_path(op)
        if path:
            touched.add(path)

    # 2. Entity references in new cards.
    for i, op in enumerate(params.operations):
        refs = extract_entity_refs(new_cards_of(op))
        if not refs:
            continue
        out.entity_refs_checked += len(refs)
        for bad in validate_refs(refs, registries):
            suggestion = ", ".join(bad.get("did_you_mean") or []) or "no close match"
            out.issues.append(
                PlanIssue(
                    severity="error",
                    code="invalid_entity_refs",
                    message=f"entity {bad['entity_id']!r} does not exist; did you mean: {suggestion}",
                    op_index=i,
                )
            )

    # 3. Card schema per op.
    for i, op in enumerate(params.operations):
        cards = new_cards_of(op)
        if not cards:
            continue
        report = validate_view({"cards": cards}, installed_custom=installed_custom)
        for issue in report.issues:
            out.issues.append(
                PlanIssue(
                    severity=issue.severity,
                    code="card_schema",
                    message=f"{issue.path}: {issue.message}",
                    op_index=i,
                )
            )

    # 4. Theme.
    if theme_names is not None:
        for i, op in enumerate(params.operations):
            theme = getattr(op, "theme", None)
            if theme and theme not in theme_names:
                out.issues.append(
                    PlanIssue(
                        severity="error",
                        code="theme_not_installed",
                        message=f"theme {theme!r} is not installed; available: {theme_names}",
                        op_index=i,
                    )
                )

    # 5. Layout lint (warnings only) on the resulting touched views.
    if out.ok:
        out.issues.extend(lint_views(out.result_config, touched, params.operations))
    return out


def lint_views(
    config: dict[str, Any], touched_paths: set[str], ops: list[PlanOp]
) -> list[PlanIssue]:
    out: list[PlanIssue] = []
    for i, op in enumerate(ops):
        if isinstance(op, CreateView) and op.layout == "masonry":
            out.append(
                PlanIssue(
                    severity="warning",
                    code="lint_masonry_view",
                    message=f"view {op.path!r} uses the legacy masonry layout",
                    op_index=i,
                )
            )

    def warn(code: str, message: str) -> None:
        out.append(PlanIssue(severity="warning", code=code, message=message))

    for view in config.get("views") or []:
        if not isinstance(view, dict) or view.get("path") not in touched_paths:
            continue
        path = view.get("path")
        entities: Counter[str] = Counter()
        if is_sections_view(view):
            for si, section in enumerate(view.get("sections") or []):
                cards = section.get("cards") or [] if isinstance(section, dict) else []
                if not cards or not is_heading_card(cards[0]):
                    warn(
                        "lint_no_heading",
                        f"view {path!r} section {si} does not start with a heading card",
                    )
                body = [c for c in cards if not is_heading_card(c)]
                if not body:
                    warn("lint_empty_section", f"view {path!r} section {si} has no cards")
                if len(body) > MAX_SECTION_CARDS:
                    warn(
                        "lint_section_too_large",
                        f"view {path!r} section {si} has {len(body)} cards; split it",
                    )
                for card in body:
                    if isinstance(card, dict) and isinstance(card.get("entity"), str):
                        entities[card["entity"]] += 1
        else:
            for card in view.get("cards") or []:
                if isinstance(card, dict) and isinstance(card.get("entity"), str):
                    entities[card["entity"]] += 1
        for entity, n in entities.items():
            if n > 1:
                warn(
                    "lint_duplicate_entity",
                    f"entity {entity} appears on {n} cards in view {path!r}",
                )
    return out
