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

"""Dashboard plan data model.

A plan is a list of operations against one storage-mode dashboard. The
model emits it via ``plan_dashboard``; the executor applies it via
``apply_dashboard_plan``. Every op addresses a view by ``path`` and a
section by index or heading text, so the plan reads the way the user
thinks about the dashboard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Position = int | Literal["start", "end"]
SectionRef = int | str

PATH_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"

_POSITION_DESC = (
    "Where to insert: 'end' (default), 'start', or a zero-based index. "
    "'start' on a section keeps its heading card first."
)
_SECTION_DESC = (
    "Section to target in a sections-layout view: zero-based index, or the "
    "heading text shown by query_dashboard. Omit for masonry views."
)


class PlanSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: str = Field(
        min_length=1,
        description=(
            "Section title. Rendered as a native heading card, prepended "
            "automatically — do not add a heading card yourself."
        ),
    )
    cards: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Card configs in display order. Per-card width is "
            "grid_options: {columns: N} (12 per section) or 'full'."
        ),
    )
    column_span: int | None = Field(
        default=None,
        ge=1,
        le=6,
        description="Widen the WHOLE section across N view columns (graph- or map-only sections).",
    )


class CreateView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["create_view"]
    title: str = Field(min_length=1)
    path: str = Field(
        pattern=PATH_PATTERN,
        description="URL slug (lowercase, digits, _ -). Required and unique within the dashboard.",
    )
    icon: str | None = None
    theme: str | None = None
    max_columns: int = Field(default=4, ge=1, le=6)
    layout: Literal["sections", "masonry"] = "sections"
    sections: list[PlanSection] = Field(default_factory=list, description="For sections layout.")
    cards: list[dict[str, Any]] = Field(
        default_factory=list, description="For masonry layout only (user asked for it)."
    )
    position: Position = Field(default="end", description=_POSITION_DESC)


class AddSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add_section"]
    view_path: str
    section: PlanSection
    position: Position = Field(default="end", description=_POSITION_DESC)


class AddCards(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add_cards"]
    view_path: str
    section: SectionRef | None = Field(default=None, description=_SECTION_DESC)
    cards: list[dict[str, Any]] = Field(min_length=1)
    position: Position = Field(default="end", description=_POSITION_DESC)


class ReplaceCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["replace_card"]
    view_path: str
    section: SectionRef | None = Field(default=None, description=_SECTION_DESC)
    card_index: int = Field(
        ge=0,
        description=(
            "Zero-based index from query_dashboard. In a section, index 0 is usually the "
            "heading. Indices shift as earlier ops in this plan apply."
        ),
    )
    card: dict[str, Any]


class RemoveCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["remove_card"]
    view_path: str
    section: SectionRef | None = Field(default=None, description=_SECTION_DESC)
    card_index: int = Field(
        ge=0,
        description=(
            "Zero-based index from query_dashboard. Indices shift as earlier ops in this "
            "plan apply."
        ),
    )


class MoveCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["move_card"]
    view_path: str
    from_section: SectionRef | None = Field(default=None, description=_SECTION_DESC)
    card_index: int = Field(
        ge=0,
        description=(
            "Index of the card to move, in from_section. Indices shift as earlier ops in "
            "this plan apply."
        ),
    )
    to_section: SectionRef | None = Field(
        default=None, description="Destination section. Defaults to from_section."
    )
    position: Position = Field(default="end", description=_POSITION_DESC)


class UpdateViewMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["update_view_meta"]
    view_path: str
    title: str | None = None
    icon: str | None = None
    theme: str | None = None
    max_columns: int | None = Field(default=None, ge=1, le=6)
    new_path: str | None = Field(default=None, pattern=PATH_PATTERN)


class RemoveSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["remove_section"]
    view_path: str
    section: SectionRef = Field(description=_SECTION_DESC)


class DeleteView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["delete_view"]
    view_path: str


PlanOp = Annotated[
    CreateView
    | AddSection
    | AddCards
    | ReplaceCard
    | RemoveCard
    | MoveCard
    | UpdateViewMeta
    | RemoveSection
    | DeleteView,
    Field(discriminator="op"),
]


class PlanDashboardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dashboard_id: str | None = Field(
        default=None, description="Dashboard url_path. null = default Overview dashboard."
    )
    summary: str = Field(
        min_length=1, max_length=200, description="One line, shown as the plan's title."
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description=(
            "Every choice made without asking (areas included, theme, card style). "
            "Shown to the user so they can correct it."
        ),
    )
    operations: list[PlanOp] = Field(
        min_length=1,
        max_length=40,
        description=(
            "Applied in order. Indices in a later op refer to the dashboard AFTER earlier "
            "ops. When removing or moving several cards from one section, list them "
            "highest index first."
        ),
    )


class CardFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    entity: str | None = None


class ResolvedTarget(BaseModel):
    """Indices an op resolved to at plan time. Card ops carry the target
    card's fingerprint so apply can refuse if the dashboard moved."""

    model_config = ConfigDict(extra="forbid")
    op_index: int
    view_index: int | None = None
    section_index: int | None = None
    section_heading: str | None = None
    to_section_index: int | None = None
    to_section_heading: str | None = None
    card_index: int | None = None
    fingerprint: CardFingerprint | None = None


class PlanIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["error", "warning"]
    code: str
    message: str
    op_index: int | None = None


class DashboardPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    dashboard_id: str | None
    summary: str
    assumptions: list[str]
    operations: list[PlanOp]
    resolved: list[ResolvedTarget]
    issues: list[PlanIssue]
    created_at: datetime
    conversation_id: str


def is_heading_card(card: Any) -> bool:
    return isinstance(card, dict) and card.get("type") == "heading"


def card_fingerprint(card: Any) -> CardFingerprint:
    """``type`` plus the primary entity — enough to tell "the tile for
    light.kitchen" from "the tile for light.hall" without comparing
    whole configs."""
    if not isinstance(card, dict):
        return CardFingerprint(type="?", entity=None)
    card_type = card.get("type")
    entity: Any = card.get("entity")
    if entity is None:
        entities = card.get("entities")
        if isinstance(entities, list) and entities:
            first = entities[0]
            if isinstance(first, str):
                entity = first
            elif isinstance(first, dict):
                entity = first.get("entity")
    return CardFingerprint(
        type=str(card_type) if card_type is not None else "?",
        entity=entity if isinstance(entity, str) else None,
    )
