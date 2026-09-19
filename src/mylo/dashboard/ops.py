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

"""Pure transforms over a Lovelace storage config.

Every function here is side-effect free: ``apply_op`` deep-copies its
input and returns a new config plus an :class:`OpReceipt` recording the
concrete indices the op landed on. No I/O, so the executor and the
validator can share the exact same code path.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, assert_never

from mylo.dashboard.plan import (
    AddCards,
    AddSection,
    CardFingerprint,
    CreateView,
    DeleteView,
    MoveCard,
    PlanOp,
    PlanSection,
    Position,
    RemoveCard,
    RemoveSection,
    ReplaceCard,
    ResolvedTarget,
    SectionRef,
    UpdateViewMeta,
    card_fingerprint,
    is_heading_card,
)


class OpError(Exception):
    """A plan op cannot be applied. ``code`` is a spec §4.10 error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TargetMismatch(OpError):
    """The card at the recorded index is not the card the plan targeted."""

    def __init__(self, op_index: int, expected: CardFingerprint, actual: CardFingerprint) -> None:
        super().__init__(
            "target_changed",
            f"op {op_index}: expected {expected.type} ({expected.entity or '-'}) at the "
            f"target index but found {actual.type} ({actual.entity or '-'})",
        )
        self.op_index = op_index
        self.expected = expected
        self.actual = actual


@dataclass(slots=True)
class OpReceipt:
    """Where an op landed. Verification compares the read-back config
    against these indices."""

    op_index: int
    op: str
    view_path: str | None = None
    view_index: int | None = None
    section_index: int | None = None
    card_indices: list[int] = field(default_factory=list)
    # For remove ops: the list length after removal.
    expected_count: int | None = None
    detail: str = ""


# ─── Lookups ────────────────────────────────────────────────────────────────


def is_sections_view(view: dict[str, Any]) -> bool:
    return view.get("type") == "sections" or isinstance(view.get("sections"), list)


def find_view_index(config: dict[str, Any], path: str) -> int | None:
    for i, view in enumerate(config.get("views") or []):
        if isinstance(view, dict) and view.get("path") == path:
            return i
    return None


def section_heading(section: Any) -> str | None:
    if not isinstance(section, dict):
        return None
    cards = section.get("cards") or []
    if cards and is_heading_card(cards[0]):
        heading = cards[0].get("heading")
        return heading if isinstance(heading, str) else None
    return None


def resolve_section(view: dict[str, Any], ref: SectionRef | None) -> int | None:
    """Index of the section ``ref`` names, or None for a masonry view."""
    if not is_sections_view(view):
        if ref is not None:
            raise OpError("section_not_applicable", "view is masonry layout; omit 'section'")
        return None
    if ref is None:
        raise OpError(
            "section_required",
            "sections-layout view: pass 'section' (index or heading text)",
        )
    sections = view.get("sections")
    if not isinstance(sections, list):
        sections = []
    if isinstance(ref, int):
        if not 0 <= ref < len(sections):
            raise OpError(
                "section_index_out_of_range",
                f"section index {ref} out of range (view has {len(sections)} sections)",
            )
        return ref
    wanted = ref.casefold()
    matches = [i for i, s in enumerate(sections) if (section_heading(s) or "").casefold() == wanted]
    if not matches:
        headings = [section_heading(s) for s in sections]
        raise OpError(
            "section_not_found", f"no section with heading {ref!r}; headings are {headings}"
        )
    if len(matches) > 1:
        raise OpError(
            "section_ambiguous",
            f"{len(matches)} sections have heading {ref!r}; use the section index",
        )
    return matches[0]


def cards_at(view: dict[str, Any], section_index: int | None) -> list[Any]:
    """The card list for a target, read-only ([] when absent)."""
    if section_index is None:
        cards = view.get("cards")
        return cards if isinstance(cards, list) else []
    sections = view.get("sections") or []
    if not 0 <= section_index < len(sections) or not isinstance(sections[section_index], dict):
        return []
    cards = sections[section_index].get("cards")
    return cards if isinstance(cards, list) else []


def _live_cards(view: dict[str, Any], section_index: int | None) -> list[Any]:
    """The card list object for a target, created if missing, so
    mutating it mutates the view."""
    if section_index is None:
        cards = view.get("cards")
        if not isinstance(cards, list):
            cards = []
            view["cards"] = cards
        return cards
    sections = view.get("sections")
    if (
        not isinstance(sections, list)
        or not 0 <= section_index < len(sections)
        or not isinstance(sections[section_index], dict)
    ):
        raise OpError(
            "section_index_out_of_range",
            f"section index {section_index} out of range (view has "
            f"{len(sections) if isinstance(sections, list) else 0} sections)",
        )
    section = sections[section_index]
    cards = section.get("cards")
    if not isinstance(cards, list):
        cards = []
        section["cards"] = cards
    return cards


def resolve_position(position: Position, length: int, *, heading_first: bool = False) -> int:
    if position == "end":
        return length
    if position == "start":
        return 1 if heading_first and length > 0 else 0
    if not 0 <= position <= length:
        raise OpError("position_out_of_range", f"position {position} out of range 0..{length}")
    return position


# ─── Builders ───────────────────────────────────────────────────────────────


def build_section(section: PlanSection) -> dict[str, Any]:
    cards = [copy.deepcopy(c) for c in section.cards]
    if cards and is_heading_card(cards[0]):
        cards[0]["heading"] = section.heading
    else:
        cards.insert(0, {"type": "heading", "heading": section.heading})
    out: dict[str, Any] = {"type": "grid", "cards": cards}
    if section.column_span is not None:
        out["column_span"] = section.column_span
    return out


def build_view(op: CreateView) -> dict[str, Any]:
    view: dict[str, Any] = {"title": op.title, "path": op.path}
    if op.icon:
        view["icon"] = op.icon
    if op.theme:
        view["theme"] = op.theme
    if op.layout == "masonry":
        view["cards"] = [copy.deepcopy(c) for c in op.cards]
        return view
    view["type"] = "sections"
    view["max_columns"] = op.max_columns
    view["sections"] = [build_section(s) for s in op.sections]
    return view


# ─── Apply ──────────────────────────────────────────────────────────────────


def _check_target(cards: list[Any], card_index: int, resolved: ResolvedTarget) -> None:
    if not 0 <= card_index < len(cards):
        raise OpError(
            "card_index_out_of_range",
            f"card index {card_index} out of range (list has {len(cards)} cards)",
        )
    if resolved.fingerprint is None:
        return
    actual = card_fingerprint(cards[card_index])
    if actual != resolved.fingerprint:
        raise TargetMismatch(resolved.op_index, resolved.fingerprint, actual)


def apply_op(
    config: dict[str, Any], op: PlanOp, resolved: ResolvedTarget
) -> tuple[dict[str, Any], OpReceipt]:
    work = copy.deepcopy(config)
    views = work.get("views")
    if not isinstance(views, list):
        views = []
        work["views"] = views
    idx = resolved.op_index

    if isinstance(op, CreateView):
        if find_view_index(work, op.path) is not None:
            raise OpError("view_exists", f"view path {op.path!r} already exists")
        at = resolve_position(op.position, len(views))
        view = build_view(op)
        views.insert(at, view)
        n_sections = len(view.get("sections") or [])
        return work, OpReceipt(
            op_index=idx,
            op="create_view",
            view_path=op.path,
            view_index=at,
            detail=f"view {op.path!r} at index {at}, {n_sections} sections",
        )

    view_index = find_view_index(work, op.view_path)
    if view_index is None:
        raise OpError("view_not_found", f"no view with path {op.view_path!r}")
    view = views[view_index]

    if isinstance(op, DeleteView):
        del views[view_index]
        return work, OpReceipt(
            op_index=idx, op="delete_view", view_path=op.view_path, detail="view deleted"
        )

    if isinstance(op, UpdateViewMeta):
        for key in ("title", "icon", "theme", "max_columns"):
            value = getattr(op, key)
            if value is not None:
                view[key] = value
        final_path = op.view_path
        if op.new_path and op.new_path != op.view_path:
            if find_view_index(work, op.new_path) is not None:
                raise OpError("view_exists", f"view path {op.new_path!r} already exists")
            view["path"] = op.new_path
            final_path = op.new_path
        return work, OpReceipt(
            op_index=idx,
            op="update_view_meta",
            view_path=final_path,
            view_index=view_index,
            detail="view metadata updated",
        )

    if isinstance(op, AddSection):
        if not is_sections_view(view):
            raise OpError("section_not_applicable", "add_section needs a sections-layout view")
        sections = view.get("sections")
        if not isinstance(sections, list):
            sections = []
            view["sections"] = sections
        at = resolve_position(op.position, len(sections))
        built = build_section(op.section)
        sections.insert(at, built)
        return work, OpReceipt(
            op_index=idx,
            op="add_section",
            view_path=op.view_path,
            view_index=view_index,
            section_index=at,
            detail=f"section {op.section.heading!r} at index {at}, {len(built['cards'])} cards",
        )

    if isinstance(op, RemoveSection):
        sections = view.get("sections") or []
        si = resolved.section_index
        if si is None or not 0 <= si < len(sections):
            raise OpError("section_index_out_of_range", f"section index {si} out of range")
        del sections[si]
        return work, OpReceipt(
            op_index=idx,
            op="remove_section",
            view_path=op.view_path,
            view_index=view_index,
            section_index=si,
            expected_count=len(sections),
            detail=f"section {si} removed, {len(sections)} remain",
        )

    if isinstance(op, AddCards):
        cards = _live_cards(view, resolved.section_index)
        at = resolve_position(
            op.position, len(cards), heading_first=bool(cards) and is_heading_card(cards[0])
        )
        new = [copy.deepcopy(c) for c in op.cards]
        cards[at:at] = new
        indices = list(range(at, at + len(new)))
        return work, OpReceipt(
            op_index=idx,
            op="add_cards",
            view_path=op.view_path,
            view_index=view_index,
            section_index=resolved.section_index,
            card_indices=indices,
            detail=f"{len(new)} cards at indices {indices}",
        )

    if isinstance(op, ReplaceCard):
        cards = _live_cards(view, resolved.section_index)
        _check_target(cards, op.card_index, resolved)
        cards[op.card_index] = copy.deepcopy(op.card)
        return work, OpReceipt(
            op_index=idx,
            op="replace_card",
            view_path=op.view_path,
            view_index=view_index,
            section_index=resolved.section_index,
            card_indices=[op.card_index],
            detail=f"card {op.card_index} replaced",
        )

    if isinstance(op, RemoveCard):
        cards = _live_cards(view, resolved.section_index)
        _check_target(cards, op.card_index, resolved)
        del cards[op.card_index]
        return work, OpReceipt(
            op_index=idx,
            op="remove_card",
            view_path=op.view_path,
            view_index=view_index,
            section_index=resolved.section_index,
            expected_count=len(cards),
            detail=f"card {op.card_index} removed, {len(cards)} remain",
        )

    if isinstance(op, MoveCard):
        src = _live_cards(view, resolved.section_index)
        _check_target(src, op.card_index, resolved)
        card = src.pop(op.card_index)
        dst = _live_cards(view, resolved.to_section_index)
        at = resolve_position(
            op.position, len(dst), heading_first=bool(dst) and is_heading_card(dst[0])
        )
        dst.insert(at, card)
        return work, OpReceipt(
            op_index=idx,
            op="move_card",
            view_path=op.view_path,
            view_index=view_index,
            section_index=resolved.to_section_index,
            card_indices=[at],
            detail=f"card moved to section {resolved.to_section_index} index {at}",
        )

    assert_never(op)
