# Dashboard Plan Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `modify_dashboard` with a plan-then-apply flow: the model produces a validated plan, the user sees it as a wireframe and approves it, and a deterministic executor applies it in one save and verifies the result against the plan.

**Architecture:** A new `mylo.dashboard` package holds the plan model, validation, pure op functions, an in-memory plan store, backups, and post-apply verification. Two tools expose it: `plan_dashboard` (read tier, uncached) and `apply_dashboard_plan` (modify tier, requires the plan id to be in the request's approved set). The UI gains a `DashboardPlanCard` and sends `approved_plan_ids` with Apply.

**Tech Stack:** Python 3.12, pydantic v2 (discriminated unions), aiohttp, pytest + pytest-asyncio, React 18 + TypeScript (Vite, Tailwind classes). Tests run with `pytest tests/unit`; lint with `ruff check src tests`, `ruff format src tests`, `mypy`; UI type-check with `cd ui && npx tsc -b --noEmit`.

**Spec:** `docs/superpowers/specs/2026-09-19-dashboard-plan-flow-design.md` §4–§8 (sub-project 2). Sub-project 1 (`2026-09-19-memory-cleanup-and-verifier-fix.md`) ships first but nothing here depends on it.

## Global Constraints

- Every commit must leave `pytest tests/unit`, `ruff check src tests`, `ruff format --check src tests`, and `mypy` green. Run all four before each commit step. UI tasks additionally run `cd ui && npx tsc -b --noEmit`.
- Do not run `npm run build` locally; the user tests the UI through a Home Assistant rebuild.
- Do not hand-bump versions or tag. Releases go through `scripts/release.sh <version>` after a `CHANGELOG.md` entry exists.
- Every new source file starts with the Apache 2.0 header block copied verbatim from any existing file in `src/` or `tests/` (the 13-line `# Copyright 2026 Maxwell Monson / Oasis Enterprise LLC ...` block; TS files use the `//` variant from `ui/src/App.tsx`).
- Async tests need no decorator; `pytest-asyncio` runs in auto mode. Tests that touch the tool registry use the `tool_registry._reset_for_tests()` autouse fixture pattern shown in Task 9.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Error codes are the exact strings in spec §4.10. Do not invent new ones without adding them to the spec.
- All dashboard writes remain storage-mode only, over the websocket commands `lovelace/config` and `lovelace/config/save`.

## File Structure

New package `src/mylo/dashboard/` (one responsibility per module):

| File | Responsibility |
|---|---|
| `__init__.py` | empty, package marker |
| `plan.py` | pydantic models for ops, plan, fingerprints, issues; `card_fingerprint`, `is_heading_card` |
| `store.py` | `PlanStore` (TTL + capacity, in memory) |
| `card_schema.py` | moved from `validators/dashboard_schema.py` + required-options table, `grid_options` check, extended known types |
| `ops.py` | pure config transforms: `build_view`, `build_section`, `resolve_section`, `resolve_position`, `apply_op`, `OpReceipt`, `OpError`, `TargetMismatch` |
| `validate.py` | `validate_plan` → resolved targets + issues + resulting config; layout lint |
| `backup.py` | `write_backup` with rotation |
| `verify.py` | `verify_plan` post-apply read-back checks |
| `io.py` | `fetch_dashboard_config`, `save_dashboard_config` (the only I/O in the package) |

Tools: `src/mylo/tools/read/plan_dashboard.py`, `src/mylo/tools/write/apply_dashboard_plan.py`. Removed: `src/mylo/tools/write/modify_dashboard.py`.

Tests: one file per module under `tests/unit/` named `test_dashboard_<module>.py`, plus `test_plan_dashboard_tool.py` and `test_apply_dashboard_plan_tool.py`.

UI: `ui/src/components/DashboardPlanCard.tsx` (new); `App.tsx`, `api.ts`, `types.ts`, `components/Composer.tsx` (modified).

---

### Task 1: Plan data model

**Files:**
- Create: `src/mylo/dashboard/__init__.py`, `src/mylo/dashboard/plan.py`
- Test: `tests/unit/test_dashboard_plan.py`

**Interfaces:**
- Produces (all in `mylo.dashboard.plan`): `Position`, `SectionRef`, `PATH_PATTERN`, `PlanSection`, `CreateView`, `AddSection`, `AddCards`, `ReplaceCard`, `RemoveCard`, `MoveCard`, `UpdateViewMeta`, `RemoveSection`, `DeleteView`, `PlanOp`, `PlanDashboardParams`, `CardFingerprint`, `ResolvedTarget`, `PlanIssue`, `DashboardPlan`, `card_fingerprint(card: Any) -> CardFingerprint`, `is_heading_card(card: Any) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dashboard_plan.py`:

```python
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
                    "sections": [{"heading": "Lights", "cards": [{"type": "tile", "entity": "light.a"}]}],
                },
                {"op": "add_cards", "view_path": "kitchen", "section": "Lights", "cards": [{"type": "tile"}]},
                {"op": "move_card", "view_path": "kitchen", "from_section": 0, "card_index": 1, "position": "start"},
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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_dashboard_plan.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mylo.dashboard'`.

- [ ] **Step 3: Create the package and models**

Create `src/mylo/dashboard/__init__.py` containing only the license header.

Create `src/mylo/dashboard/plan.py`:

```python
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
            "Zero-based index from query_dashboard. In a section, index 0 is usually the heading."
        ),
    )
    card: dict[str, Any]


class RemoveCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["remove_card"]
    view_path: str
    section: SectionRef | None = Field(default=None, description=_SECTION_DESC)
    card_index: int = Field(ge=0, description="Zero-based index from query_dashboard.")


class MoveCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["move_card"]
    view_path: str
    from_section: SectionRef | None = Field(default=None, description=_SECTION_DESC)
    card_index: int = Field(ge=0, description="Index of the card to move, in from_section.")
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
    operations: list[PlanOp] = Field(min_length=1, max_length=40)


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
    to_section_index: int | None = None
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
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_dashboard_plan.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/dashboard tests/unit/test_dashboard_plan.py
git commit -m "feat(dashboard): plan data model

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Plan store

**Files:**
- Create: `src/mylo/dashboard/store.py`
- Test: `tests/unit/test_dashboard_store.py`

**Interfaces:**
- Consumes: `DashboardPlan` from Task 1.
- Produces: `PlanStore(ttl_seconds=3600.0, capacity=50, clock=time.monotonic)` with `put(plan)`, `get(plan_id) -> DashboardPlan | None`, `remove(plan_id)`, `PlanStore.new_id() -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dashboard_store.py`:

```python
"""PlanStore: TTL expiry, capacity eviction, ids."""

from __future__ import annotations

from datetime import UTC, datetime

from mylo.dashboard.plan import DashboardPlan, DeleteView
from mylo.dashboard.store import PlanStore


def _plan(plan_id: str) -> DashboardPlan:
    return DashboardPlan(
        plan_id=plan_id,
        dashboard_id=None,
        summary="s",
        assumptions=[],
        operations=[DeleteView(op="delete_view", view_path="x")],
        resolved=[],
        issues=[],
        created_at=datetime.now(UTC),
        conversation_id="c1",
    )


def test_put_get_remove() -> None:
    store = PlanStore()
    store.put(_plan("a"))
    assert store.get("a") is not None
    assert store.get("b") is None
    store.remove("a")
    assert store.get("a") is None


def test_expires_after_ttl() -> None:
    now = [100.0]
    store = PlanStore(ttl_seconds=60.0, clock=lambda: now[0])
    store.put(_plan("a"))
    now[0] = 159.0
    assert store.get("a") is not None
    now[0] = 160.0
    assert store.get("a") is None


def test_capacity_evicts_oldest() -> None:
    now = [0.0]
    store = PlanStore(capacity=2, clock=lambda: now[0])
    for i, pid in enumerate(("a", "b", "c")):
        now[0] = float(i)
        store.put(_plan(pid))
    assert store.get("a") is None
    assert store.get("b") is not None
    assert store.get("c") is not None


def test_new_id_is_short_and_unique() -> None:
    ids = {PlanStore.new_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 8 for i in ids)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_dashboard_store.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/mylo/dashboard/store.py`:

```python
"""In-memory store for validated dashboard plans awaiting Apply.

Plans live for one hour. There is no persistence: if the add-on
restarts between plan and apply, ``apply_dashboard_plan`` returns
``plan_not_found`` and the model re-plans.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from mylo.dashboard.plan import DashboardPlan


@dataclass(slots=True)
class _Entry:
    plan: DashboardPlan
    expires_at: float


class PlanStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        capacity: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(4)

    def put(self, plan: DashboardPlan) -> None:
        self._expire()
        self._entries[plan.plan_id] = _Entry(plan=plan, expires_at=self._clock() + self._ttl)
        while len(self._entries) > self._capacity:
            oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
            del self._entries[oldest]

    def get(self, plan_id: str) -> DashboardPlan | None:
        self._expire()
        entry = self._entries.get(plan_id)
        return entry.plan if entry is not None else None

    def remove(self, plan_id: str) -> None:
        self._entries.pop(plan_id, None)

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[key]
```

- [ ] **Step 4: Run, lint, commit**

```bash
pytest tests/unit/test_dashboard_store.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/dashboard/store.py tests/unit/test_dashboard_store.py
git commit -m "feat(dashboard): in-memory plan store with TTL

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Card schema — move, required options, grid_options, known types

**Files:**
- Move: `src/mylo/validators/dashboard_schema.py` → `src/mylo/dashboard/card_schema.py`
- Modify: `src/mylo/tools/write/modify_dashboard.py:35` (import path; the file is deleted in Task 11 but must import cleanly until then)
- Test: `tests/unit/test_dashboard_schema.py` (update import; add tests)

**Interfaces:**
- Produces (in `mylo.dashboard.card_schema`): `KNOWN_NATIVE_CARD_TYPES`, `REQUIRED_OPTIONS: dict[str, tuple[tuple[str, ...], ...]]`, `validate_view(view, installed_custom) -> ValidationReport`, `has_custom_card(obj) -> bool`.

- [ ] **Step 1: Move the module and fix imports**

```bash
git mv src/mylo/validators/dashboard_schema.py src/mylo/dashboard/card_schema.py
sed -i '' 's/from mylo.validators.dashboard_schema import/from mylo.dashboard.card_schema import/' src/mylo/tools/write/modify_dashboard.py tests/unit/test_dashboard_schema.py
pytest tests/unit/test_dashboard_schema.py -q
```
Expected: PASS (pure move).

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/test_dashboard_schema.py` (above the `# ─── modify_dashboard integration` block):

```python
# ─── required options + grid_options ───────────────────────────────────────


def test_tile_without_entity_errors():
    report = validate_view({"cards": [{"type": "tile"}]}, installed_custom=None)
    assert not report.ok
    assert any("entity" in i.message and i.severity == "error" for i in report.issues)


def test_conditional_needs_conditions_and_card():
    report = validate_view({"cards": [{"type": "conditional", "card": {"type": "tile", "entity": "x.y"}}]}, installed_custom=None)
    assert not report.ok
    assert any("conditions" in i.message for i in report.issues)


def test_button_accepts_tap_action_instead_of_entity():
    report = validate_view(
        {"cards": [{"type": "button", "tap_action": {"action": "navigate", "navigation_path": "/x"}}]},
        installed_custom=None,
    )
    assert report.ok


def test_map_accepts_geo_location_sources():
    report = validate_view({"cards": [{"type": "map", "geo_location_sources": ["all"]}]}, installed_custom=None)
    assert report.ok


def test_grid_options_shape():
    ok = validate_view(
        {"cards": [{"type": "tile", "entity": "x.y", "grid_options": {"columns": "full", "rows": 2}}]},
        installed_custom=None,
    )
    assert ok.ok
    bad = validate_view(
        {"cards": [{"type": "tile", "entity": "x.y", "grid_options": {"columns": 13}}]},
        installed_custom=None,
    )
    assert not bad.ok
    assert any("grid_options.columns" in i.path for i in bad.issues)
    bad_rows = validate_view(
        {"cards": [{"type": "tile", "entity": "x.y", "grid_options": {"rows": "tall"}}]},
        installed_custom=None,
    )
    assert not bad_rows.ok


def test_energy_and_clock_cards_are_known():
    for t in ("energy-usage-graph", "energy-date-selection", "energy-sankey", "clock", "shopping-list"):
        report = validate_view({"cards": [{"type": t}]}, installed_custom=None)
        assert not any("unrecognized" in i.message for i in report.issues), t
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/unit/test_dashboard_schema.py -k "required or grid_options or energy or tile_without or conditional_needs or button_accepts or map_accepts" -v`
Expected: FAIL (no option checks yet; energy types warn).

- [ ] **Step 4: Implement**

In `src/mylo/dashboard/card_schema.py`:

1. Replace the module docstring's second paragraph ("Deliberately shallow...") with:

```
Checks that are cheap and stable across HA releases:

* every card has a ``type``
* ``custom:*`` types exist in the installed lovelace resources
* sections/stacks/conditional nesting has the right shape
* the handful of options a card cannot render without (``REQUIRED_OPTIONS``)
* ``grid_options`` shape (per-card sizing in sections layout)
```

2. Extend `KNOWN_NATIVE_CARD_TYPES` — add these members (keep the set sorted):

```
"clock", "energy-carbon-consumed-gauge", "energy-date-selection",
"energy-devices-detail-graph", "energy-devices-graph", "energy-gas-graph",
"energy-grid-neutrality-gauge", "energy-sankey", "energy-self-sufficiency-gauge",
"energy-solar-consumed-gauge", "energy-solar-graph", "energy-sources-table",
"energy-usage-graph", "energy-water-graph", "shopping-list",
```

Verify each name against the HA frontend source (`src/panels/lovelace/cards/` in home-assistant/frontend) at implementation time; drop any that do not exist and note it in the commit message.

3. Add after the set:

```python
# Options a card cannot render without. Outer tuple = requirements (all
# must hold); inner tuple = alternatives (any one satisfies). Types not
# listed are not option-checked — HA's per-card schemas churn.
REQUIRED_OPTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    **{
        t: (("entity",),)
        for t in (
            "tile",
            "entity",
            "sensor",
            "gauge",
            "thermostat",
            "humidifier",
            "light",
            "media-control",
            "weather-forecast",
            "alarm-panel",
            "plant-status",
            "picture-entity",
            "statistic",
            "todo-list",
        )
    },
    **{
        t: (("entities",),)
        for t in (
            "entities",
            "glance",
            "history-graph",
            "statistics-graph",
            "logbook",
            "calendar",
            "picture-glance",
        )
    },
    "button": (("entity", "tap_action"),),
    "conditional": (("conditions",), ("card",)),
    "vertical-stack": (("cards",),),
    "horizontal-stack": (("cards",),),
    "grid": (("cards",),),
    "markdown": (("content",),),
    "picture": (("image",),),
    "iframe": (("url",),),
    "map": (("entities", "geo_location_sources"),),
    "area": (("area",),),
}
```

4. In `_validate_card`, after the `elif card_type not in KNOWN_NATIVE_CARD_TYPES:` branch and before the recursion comment, insert:

```python
    if isinstance(card_type, str):
        for group in REQUIRED_OPTIONS.get(card_type, ()):
            if not any(card.get(key) not in (None, "", [], {}) for key in group):
                report.error(
                    path,
                    f"{card_type} card is missing required option {' or '.join(group)!s}",
                )
    _validate_grid_options(card, path, report)
```

5. Add the helper after `_validate_card`:

```python
def _validate_grid_options(card: dict[str, Any], path: str, report: ValidationReport) -> None:
    opts = card.get("grid_options")
    if opts is None:
        return
    if not isinstance(opts, dict):
        report.error(f"{path}.grid_options", "grid_options must be a mapping")
        return

    def _is_int(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    columns = opts.get("columns")
    if columns is not None and not (columns == "full" or (_is_int(columns) and 1 <= columns <= 12)):
        report.error(f"{path}.grid_options.columns", "columns must be an int 1-12 or 'full'")
    rows = opts.get("rows")
    if rows is not None and not (rows == "auto" or (_is_int(rows) and rows >= 1)):
        report.error(f"{path}.grid_options.rows", "rows must be a positive int or 'auto'")
    for key in ("min_columns", "max_columns", "min_rows", "max_rows"):
        value = opts.get(key)
        if value is not None and not (_is_int(value) and value >= 1):
            report.error(f"{path}.grid_options.{key}", f"{key} must be a positive int")
```

- [ ] **Step 5: Fix existing tests that now fail on missing options**

Run: `pytest tests/unit/test_dashboard_schema.py tests/unit/test_modify_dashboard_sections.py tests/unit/test_tools_m7b.py -q`

Any existing test that builds a `tile`/`entities` card without its required option will now error. Fix each fixture by adding the option (e.g. `{"type": "entities", "entities": ["sensor.kitchen_temp"]}`, `{"type": "tile", "entity": "light.kitchen_overhead"}`). Do not weaken the check.

- [ ] **Step 6: Lint and commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add -A src/mylo/validators src/mylo/dashboard src/mylo/tools/write/modify_dashboard.py tests/unit
git commit -m "feat(dashboard): card schema checks required options and grid_options

Moves the validator into mylo.dashboard and extends the known-types
list with HA's energy cards, clock, and shopping-list.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Entity ref extraction — more Jinja functions, dict-valued keys

**Files:**
- Modify: `src/mylo/tools/dashboard_refs.py:46-93`
- Test: `tests/unit/test_dashboard_refs.py`

**Interfaces:**
- Produces: unchanged signatures `extract_entity_refs(obj) -> set[str]`, `validate_refs(refs, registries) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_dashboard_refs.py`:

```python
def test_extracts_additional_jinja_functions():
    card = {
        "type": "markdown",
        "content": (
            "{{ has_value('sensor.a') }} {{ state_translated('sensor.b') }} "
            "{{ device_entities('abc') }} {{ area_entities('kitchen') }} "
            "{{ label_entities('x') }}"
        ),
    }
    refs = extract_entity_refs(card)
    assert {"sensor.a", "sensor.b"} <= refs


def test_extracts_from_dict_valued_entity_key():
    card = {"type": "custom:x", "entity": {"entity": "light.nested"}}
    assert "light.nested" in extract_entity_refs(card)


def test_extracts_from_list_of_dicts_under_entity_key():
    card = {"type": "custom:x", "entity_id": [{"entity": "light.a"}, "light.b"]}
    assert {"light.a", "light.b"} <= extract_entity_refs(card)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_dashboard_refs.py -v`
Expected: the three new tests FAIL.

- [ ] **Step 3: Implement**

In `src/mylo/tools/dashboard_refs.py`:

Replace `_JINJA_ENTITY_RE` with:

```python
_JINJA_ENTITY_RE = re.compile(
    r"""(?:states|is_state|state_attr|expand|has_value|state_translated)"""
    r"""\s*\(\s*['"]([a-z_]+\.[a-z0-9_]+)['"]""",
    re.IGNORECASE,
)
```

Replace the dict branch of `_walk` with:

```python
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _ENTITY_KEYS:
                if isinstance(value, str) and _ENTITY_DOMAIN_RE.match(value):
                    refs.add(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and _ENTITY_DOMAIN_RE.match(item):
                            refs.add(item)
                        else:
                            _walk(item, refs)
                else:
                    _walk(value, refs)
            else:
                _walk(value, refs)
        return
```

`device_entities`, `area_entities`, `label_entities` take ids, not entity ids, so they are deliberately not captured; the test only asserts the entity-taking functions.

- [ ] **Step 4: Run, lint, commit**

```bash
pytest tests/unit/test_dashboard_refs.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/tools/dashboard_refs.py tests/unit/test_dashboard_refs.py
git commit -m "fix(dashboard): entity ref walker covers more Jinja and nested keys

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Pure op functions

**Files:**
- Create: `src/mylo/dashboard/ops.py`
- Test: `tests/unit/test_dashboard_ops.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces (in `mylo.dashboard.ops`): `OpError(code, message)`, `TargetMismatch(op_index, expected, actual)` (subclass of `OpError`, code `target_changed`), `OpReceipt`, `is_sections_view(view)`, `find_view_index(config, path) -> int | None`, `section_heading(section) -> str | None`, `resolve_section(view, ref) -> int | None`, `cards_at(view, section_index) -> list`, `resolve_position(position, length, *, heading_first=False) -> int`, `build_section(section: PlanSection) -> dict`, `build_view(op: CreateView) -> dict`, `apply_op(config, op, resolved) -> tuple[dict, OpReceipt]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dashboard_ops.py`:

```python
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
        CreateView(op="create_view", title="K", path="k", layout="masonry", cards=[{"type": "tile"}])
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
    out, receipt = apply_op(_config(), op, ResolvedTarget(op_index=0, view_index=1, section_index=0))
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
    bad = good.model_copy(update={"fingerprint": CardFingerprint(type="tile", entity="light.other")})
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
    out, _ = apply_op(out, DeleteView(op="delete_view", view_path="home"), ResolvedTarget(op_index=1))
    assert find_view_index(out, "home") is None
    with pytest.raises(OpError) as exc:
        apply_op(out, DeleteView(op="delete_view", view_path="home"), ResolvedTarget(op_index=2))
    assert exc.value.code == "view_not_found"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_dashboard_ops.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/mylo/dashboard/ops.py`:

```python
"""Pure transforms over a Lovelace storage config.

Every function here is side-effect free: ``apply_op`` deep-copies its
input and returns a new config plus an :class:`OpReceipt` recording the
concrete indices the op landed on. No I/O, so the executor and the
validator can share the exact same code path.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

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
    matches = [
        i for i, s in enumerate(sections) if (section_heading(s) or "").casefold() == wanted
    ]
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
    section = view["sections"][section_index]
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


def _check_target(
    cards: list[Any], card_index: int, resolved: ResolvedTarget
) -> None:
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

    raise OpError("invalid_op", f"unsupported op {type(op).__name__}")
```

- [ ] **Step 4: Run, lint, commit**

```bash
pytest tests/unit/test_dashboard_ops.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/dashboard/ops.py tests/unit/test_dashboard_ops.py
git commit -m "feat(dashboard): pure op functions with receipts and fingerprint guards

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Plan validation

**Files:**
- Create: `src/mylo/dashboard/validate.py`
- Test: `tests/unit/test_dashboard_validate.py`

**Interfaces:**
- Consumes: Task 1 models, Task 3 `validate_view`, Task 4 `extract_entity_refs`/`validate_refs`, Task 5 ops.
- Produces (in `mylo.dashboard.validate`): `PlanValidation` dataclass (`resolved`, `issues`, `result_config`, `entity_refs_checked`, `.ok`), `new_cards_of(op) -> list[dict]`, `resolve_target(config, op, op_index) -> ResolvedTarget`, `validate_plan(params, current_config, *, registries, installed_custom, theme_names) -> PlanValidation`, `lint_views(config, touched_paths, ops) -> list[PlanIssue]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dashboard_validate.py`:

```python
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
            {"op": "add_cards", "view_path": "k", "section": "A", "cards": [{"type": "tile", "entity": "sensor.temp"}]},
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
            {"op": "add_cards", "view_path": "nope", "cards": [{"type": "tile", "entity": "light.hall"}]},
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
    assert _codes(_run(_params({"op": "add_cards", "view_path": "rooms", "cards": [{"type": "tile", "entity": "light.hall"}]}))) == ["section_required"]
    assert _codes(_run(_params({"op": "add_cards", "view_path": "rooms", "section": "Garage", "cards": [{"type": "tile", "entity": "light.hall"}]}))) == ["section_not_found"]
    assert _codes(_run(_params({"op": "add_cards", "view_path": "rooms", "section": 3, "cards": [{"type": "tile", "entity": "light.hall"}]}))) == ["section_index_out_of_range"]


def test_card_index_out_of_range_and_fingerprint_recorded() -> None:
    v = _run(_params({"op": "remove_card", "view_path": "rooms", "section": 0, "card_index": 9}))
    assert _codes(v) == ["card_index_out_of_range"]
    v = _run(_params({"op": "remove_card", "view_path": "rooms", "section": "Lights", "card_index": 1}))
    assert v.ok
    fp = v.resolved[0].fingerprint
    assert fp is not None and fp.type == "tile" and fp.entity == "light.kitchen"


def test_move_card_resolves_both_sections() -> None:
    config = _config()
    config["views"][0]["sections"].append(
        {"type": "grid", "cards": [{"type": "heading", "heading": "Climate"}]}
    )
    v = _run(
        _params({"op": "move_card", "view_path": "rooms", "from_section": "Lights", "card_index": 1, "to_section": "Climate", "position": "start"}),
        config,
    )
    assert v.ok, v.issues
    assert v.resolved[0].section_index == 0
    assert v.resolved[0].to_section_index == 1
    assert v.result_config["views"][0]["sections"][1]["cards"][1]["entity"] == "light.kitchen"


def test_position_out_of_range() -> None:
    v = _run(_params({"op": "add_section", "view_path": "rooms", "section": {"heading": "X"}, "position": 5}))
    assert _codes(v) == ["position_out_of_range"]


def test_invalid_entity_refs_with_suggestion() -> None:
    v = _run(_params({"op": "add_cards", "view_path": "rooms", "section": 0, "cards": [{"type": "tile", "entity": "light.kitchn"}]}))
    assert _codes(v) == ["invalid_entity_refs"]
    assert "light.kitchen" in v.issues[0].message
    assert v.issues[0].op_index == 0


def test_card_schema_errors_block() -> None:
    v = _run(_params({"op": "add_cards", "view_path": "rooms", "section": 0, "cards": [{"type": "tile"}]}))
    assert "card_schema" in _codes(v)
    assert not v.ok


def test_custom_card_not_installed_blocks_when_resources_known() -> None:
    v = _run(
        _params({"op": "add_cards", "view_path": "rooms", "section": 0, "cards": [{"type": "custom:nope-card", "entity": "light.hall"}]}),
        installed_custom={"custom:mushroom-light-card"},
    )
    assert not v.ok
    v = _run(
        _params({"op": "add_cards", "view_path": "rooms", "section": 0, "cards": [{"type": "custom:nope-card", "entity": "light.hall"}]}),
        installed_custom=None,
    )
    assert v.ok and any(i.severity == "warning" for i in v.issues)


def test_theme_not_installed() -> None:
    v = _run(_params({"op": "create_view", "title": "K", "path": "k", "theme": "noctis"}), theme_names=["ios"])
    assert _codes(v) == ["theme_not_installed"]
    v = _run(_params({"op": "create_view", "title": "K", "path": "k", "theme": "noctis"}), theme_names=None)
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
                    {"heading": "Dup", "cards": [{"type": "tile", "entity": "sensor.temp"}, {"type": "tile", "entity": "sensor.temp"}]},
                ],
            },
            {"op": "add_cards", "view_path": "k", "section": "Empty", "cards": [{"type": "tile", "entity": "climate.main"}], "position": 0},
            {"op": "create_view", "title": "M", "path": "m", "layout": "masonry"},
        )
    )
    assert v.ok
    codes = set(_codes(v))
    assert {"lint_empty_section", "lint_section_too_large", "lint_duplicate_entity", "lint_no_heading", "lint_masonry_view"} <= codes
    assert all(i.severity == "warning" for i in v.issues)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_dashboard_validate.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/mylo/dashboard/validate.py`:

```python
"""Validate a dashboard plan against the live config.

Runs the stages in spec §4.3, in order, stopping at the first stage
that produces an error. Target resolution applies each op to a working
copy so later ops can address views and sections created earlier in
the same plan.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

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
    raise OpError("invalid_op", f"unsupported op {type(op).__name__}")


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
                    warn("lint_no_heading", f"view {path!r} section {si} does not start with a heading card")
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
                warn("lint_duplicate_entity", f"entity {entity} appears on {n} cards in view {path!r}")
    return out
```

- [ ] **Step 4: Run, lint, commit**

```bash
pytest tests/unit/test_dashboard_validate.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/dashboard/validate.py tests/unit/test_dashboard_validate.py
git commit -m "feat(dashboard): plan validation with target resolution and lint

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Backup, verification, and I/O helpers

**Files:**
- Create: `src/mylo/dashboard/backup.py`, `src/mylo/dashboard/verify.py`, `src/mylo/dashboard/io.py`
- Test: `tests/unit/test_dashboard_backup.py`, `tests/unit/test_dashboard_verify.py`

**Interfaces:**
- Produces: `write_backup(mylo_data_dir: Path, dashboard_id: str | None, config: dict, *, keep: int = 20, now: datetime | None = None) -> Path`; `verify_plan(plan: DashboardPlan, receipts: list[OpReceipt], expected_config: dict, config_after: dict) -> dict`; `fetch_dashboard_config(ws_client, dashboard_id) -> dict` raising `DashboardUnavailable(code, message)` / `DashboardNotFound`; `save_dashboard_config(ws_client, dashboard_id, config) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dashboard_backup.py`:

```python
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mylo.dashboard.backup import write_backup


def test_backup_written_and_rotated(tmp_path: Path) -> None:
    base = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
    paths = [
        write_backup(tmp_path, None, {"views": [{"n": i}]}, keep=3, now=base + timedelta(seconds=i))
        for i in range(5)
    ]
    folder = tmp_path / "dashboard_backups" / "default"
    remaining = sorted(folder.glob("*.json"))
    assert len(remaining) == 3
    assert remaining[-1] == paths[-1]
    assert json.loads(paths[-1].read_text())["views"] == [{"n": 4}]
    assert not paths[0].exists()


def test_backup_uses_dashboard_id_folder(tmp_path: Path) -> None:
    p = write_backup(tmp_path, "tablet", {"views": []})
    assert p.parent == tmp_path / "dashboard_backups" / "tablet"
```

Create `tests/unit/test_dashboard_verify.py`:

```python
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
        e: EntityEntry.from_raw({"entity_id": e})
        for e in ("light.a", "light.b", "climate.c")
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
    {"op": "create_view", "title": "K", "path": "k", "position": "start",
     "sections": [{"heading": "A", "cards": [{"type": "tile", "entity": "light.a"}]}]},
    {"op": "add_section", "view_path": "rooms", "section": {"heading": "Media", "cards": [{"type": "tile", "entity": "light.b"}]}, "position": 1},
    {"op": "add_cards", "view_path": "rooms", "section": "Climate", "cards": [{"type": "thermostat", "entity": "climate.c"}]},
    {"op": "move_card", "view_path": "rooms", "from_section": "Lights", "card_index": 2, "to_section": "Climate", "position": "start"},
    {"op": "replace_card", "view_path": "rooms", "section": "Lights", "card_index": 1, "card": {"type": "light", "entity": "light.a"}},
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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_dashboard_backup.py tests/unit/test_dashboard_verify.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement backup**

Create `src/mylo/dashboard/backup.py`:

```python
"""Pre-apply snapshots of a dashboard config.

Storage-mode dashboards have no file for the rollback machinery in
``mylo.files`` to protect, so the executor writes its own JSON
snapshot before every save and keeps the newest ``keep`` per dashboard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mylo.files.manager import atomic_write

BACKUP_DIRNAME = "dashboard_backups"


def write_backup(
    mylo_data_dir: Path,
    dashboard_id: str | None,
    config: dict[str, Any],
    *,
    keep: int = 20,
    now: datetime | None = None,
) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
    folder = mylo_data_dir / BACKUP_DIRNAME / (dashboard_id or "default")
    path = folder / f"{stamp}.json"
    atomic_write(path, json.dumps(config, indent=2, sort_keys=True))
    existing = sorted(folder.glob("*.json"))
    for old in existing[: max(0, len(existing) - keep)]:
        old.unlink(missing_ok=True)
    return path
```

- [ ] **Step 4: Implement I/O**

Create `src/mylo/dashboard/io.py`:

```python
"""The only two websocket calls the dashboard package makes."""

from __future__ import annotations

from typing import Any, Protocol

from mylo.ha.ws_client import CommandError


class _WsClient(Protocol):
    async def send_command(self, type_: str, **kwargs: Any) -> Any: ...


class DashboardUnavailable(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DashboardNotFound(DashboardUnavailable):
    pass


async def fetch_dashboard_config(ws_client: _WsClient, dashboard_id: str | None) -> dict[str, Any]:
    """Current config. The default dashboard with nothing saved yet reads
    as an empty config; a named dashboard that doesn't exist raises."""
    try:
        result = await ws_client.send_command("lovelace/config", url_path=dashboard_id)
    except CommandError as exc:
        if exc.code in ("config_not_found", "not_found"):
            if dashboard_id is None:
                return {"views": []}
            raise DashboardNotFound(exc.code, f"no dashboard {dashboard_id!r}") from exc
        raise DashboardUnavailable(exc.code, exc.message) from exc
    if not isinstance(result, dict):
        return {"views": []}
    return result


async def save_dashboard_config(
    ws_client: _WsClient, dashboard_id: str | None, config: dict[str, Any]
) -> None:
    await ws_client.send_command(
        "lovelace/config/save", write=True, url_path=dashboard_id, config=config
    )
```

- [ ] **Step 5: Implement verify**

Create `src/mylo/dashboard/verify.py`:

```python
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
                return False, f"section {si} heading is {section_heading(section)!r}, expected {planned.heading!r}"
            expected = len(build_section(planned)["cards"])
            actual = len(section.get("cards") or [])
            if actual != expected:
                return False, f"section {si} has {actual} cards, expected {expected}"
        return True, f"view {path!r} at index {vi}, {len(sections)} sections"

    if isinstance(op, AddSection):
        sections = view.get("sections") or []
        si = receipt.section_index
        if si is None or not 0 <= si < len(sections):
            return False, f"section index {si} missing"
        if section_heading(sections[si]) != op.section.heading:
            return False, f"section {si} heading is {section_heading(sections[si])!r}, expected {op.section.heading!r}"
        expected = len(build_section(op.section)["cards"])
        actual = len(sections[si].get("cards") or [])
        if actual != expected:
            return False, f"section {si} has {actual} cards, expected {expected}"
        return True, f"section {op.section.heading!r} at index {si}, {actual} cards"

    if isinstance(op, AddCards):
        cards = cards_at(view, receipt.section_index)
        for idx, planned in zip(receipt.card_indices, op.cards, strict=True):
            want = card_fingerprint(planned)
            if idx >= len(cards) or card_fingerprint(cards[idx]) != want:
                return False, f"card at index {idx} is not the planned {want.type} ({want.entity or '-'})"
        return True, f"{len(op.cards)} cards at indices {receipt.card_indices}"

    if isinstance(op, ReplaceCard):
        cards = cards_at(view, receipt.section_index)
        want = card_fingerprint(op.card)
        if op.card_index < len(cards) and card_fingerprint(cards[op.card_index]) == want:
            return True, f"card {op.card_index} is now {want.type} ({want.entity or '-'})"
        return False, f"card {op.card_index} is not the replacement {want.type}"

    if isinstance(op, RemoveCard | RemoveSection):
        if isinstance(op, RemoveCard):
            actual = len(cards_at(view, receipt.section_index))
            noun = "cards"
        else:
            actual = len(view.get("sections") or [])
            noun = "sections"
        if actual == receipt.expected_count:
            return True, f"{actual} {noun} remain"
        return False, f"{actual} {noun} remain, expected {receipt.expected_count}"

    if isinstance(op, MoveCard):
        dst = cards_at(view, receipt.section_index)
        fp = plan.resolved[receipt.op_index].fingerprint
        idx = receipt.card_indices[0] if receipt.card_indices else -1
        if fp is not None and 0 <= idx < len(dst) and card_fingerprint(dst[idx]) == fp:
            return True, f"{fp.type} ({fp.entity or '-'}) at section {receipt.section_index} index {idx}"
        return False, f"moved card not at section {receipt.section_index} index {idx}"

    if isinstance(op, UpdateViewMeta):
        for key in ("title", "icon", "theme", "max_columns"):
            value = getattr(op, key)
            if value is not None and view.get(key) != value:
                return False, f"{key} is {view.get(key)!r}, expected {value!r}"
        return True, "view metadata matches"

    return False, f"no verifier for {receipt.op}"
```

- [ ] **Step 6: Run, lint, commit**

```bash
pytest tests/unit/test_dashboard_backup.py tests/unit/test_dashboard_verify.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/dashboard/backup.py src/mylo/dashboard/verify.py src/mylo/dashboard/io.py tests/unit/test_dashboard_backup.py tests/unit/test_dashboard_verify.py
git commit -m "feat(dashboard): backups, read-back verification, and io helpers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Plumbing — uncached tools, plan store on the context, approved plan ids

**Files:**
- Modify: `src/mylo/tools/base.py:118-124` (`ToolDefinition` fields)
- Modify: `src/mylo/tools/executor.py:143-148`
- Modify: `src/mylo/llm/tool_loop.py:59-65`
- Modify: `src/mylo/tools/context.py`
- Modify: `src/mylo/server/app.py:59-70` (AppKeys), `:250-257` (ToolContext)
- Modify: `src/mylo/server/routes_chat.py:346-392`
- Modify: `tests/unit/_helpers.py:41-62` (`make_ctx`)
- Test: `tests/unit/test_executor.py`, new `tests/unit/test_routes_chat_approval.py`

**Interfaces:**
- Produces: `ToolDefinition.cacheable: bool = True`; `ToolContext.plans: PlanStore | None = None`; `ToolContext.approved_plan_ids: frozenset[str] = frozenset()`; `AppKeys.PLANS`; chat request body field `approved_plan_ids: list[str]`; `make_ctx(..., plans=None, approved_plan_ids=frozenset())`.

- [ ] **Step 1: Write the failing executor test**

Append to `tests/unit/test_executor.py`:

```python
async def test_uncacheable_read_tool_runs_every_time(tmp_path: Path) -> None:
    calls: list[int] = []

    async def _count(params: _P, _ctx: Any) -> ToolResult:
        calls.append(params.n)
        return ToolResult.ok({"n": params.n})

    t = ToolDefinition(
        name="nocache", description="t", params_model=_P, tier=Tier.READ, handler=_count, cacheable=False
    )
    tool_registry.register(t)
    await execute("nocache", {"n": 1}, _ctx(tmp_path))
    await execute("nocache", {"n": 1}, _ctx(tmp_path))
    assert calls == [1, 1]


async def test_cacheable_read_tool_reuses_result(tmp_path: Path) -> None:
    calls: list[int] = []

    async def _count(params: _P, _ctx: Any) -> ToolResult:
        calls.append(params.n)
        return ToolResult.ok({"n": params.n})

    _define("cached", _count)
    await execute("cached", {"n": 2}, _ctx(tmp_path))
    await execute("cached", {"n": 2}, _ctx(tmp_path))
    assert calls == [2]
```

Check whether `test_executor.py` already clears the result cache between tests; if the second test fails because of a stale cache from another test, add `from mylo.tools import executor as _executor` and `_executor._result_cache.clear()` to the autouse fixture.

- [ ] **Step 2: Write the failing routes test**

Create `tests/unit/test_routes_chat_approval.py`:

```python
"""The chat route threads approved_plan_ids into the per-turn ToolContext."""

from __future__ import annotations

from mylo.server.routes_chat import _approved_plan_ids_from_body


def test_parses_string_list() -> None:
    assert _approved_plan_ids_from_body({"approved_plan_ids": ["a", "b"]}) == frozenset({"a", "b"})


def test_ignores_garbage() -> None:
    assert _approved_plan_ids_from_body({}) == frozenset()
    assert _approved_plan_ids_from_body({"approved_plan_ids": "a"}) == frozenset()
    assert _approved_plan_ids_from_body({"approved_plan_ids": ["a", 3, None]}) == frozenset({"a"})
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/unit/test_executor.py tests/unit/test_routes_chat_approval.py -v`
Expected: FAIL (`cacheable` unexpected kwarg; import error for `_approved_plan_ids_from_body`).

- [ ] **Step 4: Implement**

`src/mylo/tools/base.py` — in `ToolDefinition`, after `handler: ToolHandler[Params]` add:

```python
    # READ tools are cached for 120s by the executor. Tools whose result
    # must be fresh every call (plan_dashboard mints a new plan id;
    # ask_user pauses the turn) opt out.
    cacheable: bool = True
```

`src/mylo/tools/executor.py` — change `if tool.tier == Tier.READ:` (the cache check) to `if tool.tier == Tier.READ and tool.cacheable:`. The cache *put* at the end of `execute` is already guarded by `cache_key is not None`, which is only set on the cacheable path, so it needs no change.

`src/mylo/llm/tool_loop.py` — in `_read_call_key`, change the guard to:

```python
    if tool_def is None or tool_def.tier != Tier.READ or not tool_def.cacheable:
        return None
```

`src/mylo/tools/context.py` — add imports and fields:

```python
from mylo.dashboard.store import PlanStore
```

and at the end of the dataclass:

```python
    # Plan ids the user approved by clicking Apply on this request. Only
    # apply_dashboard_plan reads it; a plan id not in this set is refused.
    approved_plan_ids: frozenset[str] = field(default_factory=frozenset)
    # Process-wide store of validated dashboard plans awaiting Apply.
    plans: PlanStore | None = None
```

`src/mylo/server/app.py`:
- Add `PLANS = web.AppKey("plans", PlanStore)` to `AppKeys` (import `PlanStore` from `mylo.dashboard.store`).
- Before `app[AppKeys.TOOL_CONTEXT] = ToolContext(...)` add `plan_store = PlanStore()` and `app[AppKeys.PLANS] = plan_store`; pass `plans=plan_store` into the `ToolContext(...)` call.

`src/mylo/server/routes_chat.py`:
- Add a module-level helper near the top of the handlers:

```python
def _approved_plan_ids_from_body(body: dict[str, Any]) -> frozenset[str]:
    raw = body.get("approved_plan_ids")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(p for p in raw if isinstance(p, str) and p)
```

- After `approved = bool(body.get("approved", False))` add `approved_plan_ids = _approved_plan_ids_from_body(body)`.
- In the per-turn `ToolContext(...)` add `approved_plan_ids=approved_plan_ids, plans=base_ctx.plans,`.

`tests/unit/_helpers.py` — extend `make_ctx`:

```python
def make_ctx(
    *,
    ws_client: Any,
    registries: Registries,
    tmp_path: Path,
    conversation_id: str = "test",
    user_approved: bool = False,
    dry_run: bool = False,
    plans: PlanStore | None = None,
    approved_plan_ids: frozenset[str] = frozenset(),
) -> ToolContext:
    config = make_config(tmp_path)
    return ToolContext(
        ws_client=ws_client,
        registries=registries,
        config=config,
        permissions=default_permissions(),
        audit=AuditLogger(config.mylo_data_dir),
        conversation_id=conversation_id,
        user_approved=user_approved,
        dry_run=dry_run,
        plans=plans,
        approved_plan_ids=approved_plan_ids,
    )
```

with `from mylo.dashboard.store import PlanStore` added to its imports.

Also check `src/mylo/__main__.py` for a `ToolContext(` construction (`grep -n "ToolContext(" src/mylo/__main__.py`). If one exists, pass `plans=PlanStore()` there too so the CLI can plan.

- [ ] **Step 5: Run, lint, commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/tools/base.py src/mylo/tools/executor.py src/mylo/llm/tool_loop.py src/mylo/tools/context.py src/mylo/server/app.py src/mylo/server/routes_chat.py src/mylo/__main__.py tests/unit/_helpers.py tests/unit/test_executor.py tests/unit/test_routes_chat_approval.py
git commit -m "feat(tools): cacheable flag, plan store on context, approved_plan_ids

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: `plan_dashboard` tool

**Files:**
- Create: `src/mylo/tools/read/plan_dashboard.py`
- Modify: `src/mylo/tools/registry.py:36-48` (add module to `_DEFAULT_MODULES`)
- Test: `tests/unit/test_plan_dashboard_tool.py`

**Interfaces:**
- Consumes: Tasks 1–8.
- Produces: tool `plan_dashboard` (READ, `cacheable=False`) returning `{preview: true, plan_id, plan, entity_refs_validated, issues, note}` or error codes `plans_unavailable`, `dashboard_not_found`, `dashboard_unavailable`, `plan_invalid`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_plan_dashboard_tool.py`:

```python
"""plan_dashboard: fetch, validate, stage, and the error envelopes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.dashboard.store import PlanStore
from mylo.ha.registries import EntityEntry, Registries
from mylo.ha.ws_client import CommandError
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ in self._responses:
            val = self._responses[type_]
            if isinstance(val, Exception):
                raise val
            if callable(val):
                return val(**kwargs)
            return val
        return {}


def _registries() -> Registries:
    reg = Registries()
    reg.entities = {
        e: EntityEntry.from_raw({"entity_id": e, "original_name": e})
        for e in ("light.kitchen", "sensor.temp")
    }
    return reg


_DASHBOARD = {
    "views": [
        {
            "path": "rooms",
            "title": "Rooms",
            "type": "sections",
            "sections": [
                {"type": "grid", "cards": [{"type": "heading", "heading": "Lights"}]},
            ],
        }
    ]
}


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


def _ctx(tmp_path: Path, responses: dict[str, Any] | None = None, *, plans: PlanStore | None = None):
    client = _FakeClient({"lovelace/config": _DASHBOARD, **(responses or {})})
    return make_ctx(
        ws_client=client, registries=_registries(), tmp_path=tmp_path, plans=plans or PlanStore()
    )


def _params(*ops: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"summary": "test plan", "operations": list(ops), **extra}


async def test_valid_plan_is_staged(tmp_path: Path) -> None:
    store = PlanStore()
    ctx = _ctx(tmp_path, plans=store)
    result = await execute(
        "plan_dashboard",
        _params(
            {"op": "add_cards", "view_path": "rooms", "section": "Lights", "cards": [{"type": "tile", "entity": "light.kitchen"}]},
            assumptions=["only kitchen entities"],
        ),
        ctx,
    )
    assert result.status.value == "ok", result.error_message
    data = result.data
    assert data["preview"] is True
    assert data["entity_refs_validated"] == 1
    assert data["plan"]["assumptions"] == ["only kitchen entities"]
    assert data["plan"]["resolved"][0]["section_index"] == 0
    assert store.get(data["plan_id"]) is not None
    assert not any(t == "lovelace/config/save" for t, _ in ctx.ws_client.calls)


async def test_invalid_plan_not_staged(tmp_path: Path) -> None:
    store = PlanStore()
    ctx = _ctx(tmp_path, plans=store)
    result = await execute(
        "plan_dashboard",
        _params({"op": "add_cards", "view_path": "rooms", "section": "Lights", "cards": [{"type": "tile", "entity": "light.kitchn"}]}),
        ctx,
    )
    assert result.error_code == "plan_invalid"
    assert result.data["issues"][0]["code"] == "invalid_entity_refs"
    assert "light.kitchen" in result.data["issues"][0]["message"]
    assert store._entries == {}


async def test_two_calls_get_distinct_plan_ids(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    params = _params({"op": "delete_view", "view_path": "rooms"})
    a = await execute("plan_dashboard", params, ctx)
    b = await execute("plan_dashboard", params, ctx)
    assert a.data["plan_id"] != b.data["plan_id"]


async def test_named_dashboard_not_found(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"lovelace/config": CommandError("config_not_found", "nope")})
    result = await execute(
        "plan_dashboard",
        _params({"op": "create_view", "title": "K", "path": "k"}, dashboard_id="tablet"),
        ctx,
    )
    assert result.error_code == "dashboard_not_found"


async def test_default_dashboard_without_config_plans_against_empty(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"lovelace/config": CommandError("config_not_found", "nope")})
    result = await execute(
        "plan_dashboard", _params({"op": "create_view", "title": "K", "path": "k"}), ctx
    )
    assert result.status.value == "ok"


async def test_theme_checked_when_listable(tmp_path: Path) -> None:
    themes = {"themes": {"noctis": {}}, "default_theme": "noctis"}
    ctx = _ctx(tmp_path, {"frontend/get_themes": themes})
    bad = await execute(
        "plan_dashboard",
        _params({"op": "create_view", "title": "K", "path": "k", "theme": "ios"}),
        ctx,
    )
    assert bad.error_code == "plan_invalid"
    assert bad.data["issues"][0]["code"] == "theme_not_installed"
    good = await execute(
        "plan_dashboard",
        _params({"op": "create_view", "title": "K", "path": "k", "theme": "noctis"}),
        ctx,
    )
    assert good.status.value == "ok"


async def test_theme_skipped_when_unavailable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"frontend/get_themes": CommandError("unknown_command", "no")})
    result = await execute(
        "plan_dashboard",
        _params({"op": "create_view", "title": "K", "path": "k", "theme": "anything"}),
        ctx,
    )
    assert result.status.value == "ok"


async def test_custom_card_triggers_resource_lookup(tmp_path: Path) -> None:
    resources = [{"url": "/hacsfiles/mushroom/mushroom.js", "type": "module"}]
    ctx = _ctx(tmp_path, {"lovelace/resources": resources})
    result = await execute(
        "plan_dashboard",
        _params({"op": "add_cards", "view_path": "rooms", "section": 0, "cards": [{"type": "custom:nope-card", "entity": "light.kitchen"}]}),
        ctx,
    )
    assert result.error_code == "plan_invalid"
    assert any(t == "lovelace/resources" for t, _ in ctx.ws_client.calls)


async def test_native_only_plan_skips_resource_lookup(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    await execute(
        "plan_dashboard",
        _params({"op": "add_cards", "view_path": "rooms", "section": 0, "cards": [{"type": "tile", "entity": "light.kitchen"}]}),
        ctx,
    )
    assert not any(t == "lovelace/resources" for t, _ in ctx.ws_client.calls)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_plan_dashboard_tool.py -v`
Expected: FAIL with `unknown_tool` errors.

- [ ] **Step 3: Implement the tool**

Create `src/mylo/tools/read/plan_dashboard.py`:

```python
"""``plan_dashboard`` — validate a dashboard change and stage it for approval.

Nothing is written. The plan is stored server-side under a short id;
the UI renders it as a wireframe with Apply / Modify, and the Apply
click sends the id back as ``approved_plan_ids`` so
``apply_dashboard_plan`` can prove the user saw exactly this plan.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mylo.dashboard.card_schema import has_custom_card
from mylo.dashboard.io import (
    DashboardNotFound,
    DashboardUnavailable,
    fetch_dashboard_config,
)
from mylo.dashboard.plan import DashboardPlan, PlanDashboardParams
from mylo.dashboard.store import PlanStore
from mylo.dashboard.validate import new_cards_of, validate_plan
from mylo.ha.lovelace_meta import detect_custom_cards, get_resources, get_themes
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


async def handler(params: PlanDashboardParams, ctx: ToolContext) -> ToolResult:
    if ctx.plans is None:
        return ToolResult.error("plans_unavailable", "plan store not configured")

    try:
        current = await fetch_dashboard_config(ctx.ws_client, params.dashboard_id)
    except DashboardNotFound as exc:
        return ToolResult.error("dashboard_not_found", exc.message)
    except DashboardUnavailable as exc:
        return ToolResult.error("dashboard_unavailable", f"{exc.code}: {exc.message}")

    new_cards = [c for op in params.operations for c in new_cards_of(op)]
    installed_custom: set[str] | None = None
    if has_custom_card(new_cards):
        resources = await get_resources(ctx.ws_client)
        if resources is not None:
            installed_custom = detect_custom_cards(resources)

    theme_names: list[str] | None = None
    if any(getattr(op, "theme", None) for op in params.operations):
        themes = await get_themes(ctx.ws_client)
        theme_names = themes.names if themes is not None else None

    validation = validate_plan(
        params,
        current,
        registries=ctx.registries,
        installed_custom=installed_custom,
        theme_names=theme_names,
    )
    issues = [i.model_dump(exclude_none=True) for i in validation.issues]
    if not validation.ok:
        n_errors = sum(1 for i in validation.issues if i.severity == "error")
        return ToolResult.error(
            "plan_invalid",
            f"{n_errors} error(s) in the plan — fix every listed issue and call "
            "plan_dashboard again",
            data={"issues": issues},
        )

    plan = DashboardPlan(
        plan_id=PlanStore.new_id(),
        dashboard_id=params.dashboard_id,
        summary=params.summary,
        assumptions=params.assumptions,
        operations=params.operations,
        resolved=validation.resolved,
        issues=validation.issues,
        created_at=datetime.now(UTC),
        conversation_id=ctx.conversation_id,
    )
    ctx.plans.put(plan)
    return ToolResult.ok(
        {
            "preview": True,
            "plan_id": plan.plan_id,
            "plan": plan.model_dump(mode="json"),
            "entity_refs_validated": validation.entity_refs_checked,
            "issues": issues,
            "note": (
                "Plan staged. Tell the user it is ready to review, then END the turn. "
                "Call apply_dashboard_plan only after their next message approves it."
            ),
        }
    )


TOOL = ToolDefinition(
    name="plan_dashboard",
    description=(
        "Stage a dashboard change for the user's approval. Pass every operation "
        "for the change in one call: create_view (sections with headings), "
        "add_section, add_cards, replace_card, remove_card, move_card, "
        "update_view_meta, remove_section, delete_view. Sections are addressed "
        "by heading text or index; 'position' is 'start', 'end', or an index. "
        "Validates entity ids, card options, custom cards, and theme; returns "
        "plan_invalid with a fix list, or a plan_id the user must approve by "
        "clicking Apply. Nothing is written here. List every unasked choice in "
        "'assumptions'."
    ),
    params_model=PlanDashboardParams,
    tier=Tier.READ,
    handler=handler,
    cacheable=False,
)
register(TOOL)
```

Add `"mylo.tools.read.plan_dashboard",` to `_DEFAULT_MODULES` in `src/mylo/tools/registry.py` directly after `"mylo.tools.read.query_dashboard_env",`.

- [ ] **Step 4: Run, lint, commit**

```bash
pytest tests/unit/test_plan_dashboard_tool.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/tools/read/plan_dashboard.py src/mylo/tools/registry.py tests/unit/test_plan_dashboard_tool.py
git commit -m "feat(tools): plan_dashboard stages a validated plan for approval

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: `apply_dashboard_plan` tool

**Files:**
- Create: `src/mylo/tools/write/apply_dashboard_plan.py`
- Modify: `src/mylo/tools/registry.py` (add module after `"mylo.tools.write.modify_dashboard",`)
- Test: `tests/unit/test_apply_dashboard_plan_tool.py`

**Interfaces:**
- Produces: tool `apply_dashboard_plan(plan_id)` (MODIFY) returning `{preview: false, plan_id, applied, backup, verification}` or error codes `plan_not_approved`, `plans_unavailable`, `plan_not_found`, `dashboard_unavailable`, `target_changed`, `ha_error`, plus any `OpError` code.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_apply_dashboard_plan_tool.py`:

```python
"""apply_dashboard_plan: approval binding, single save, backup, verify."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mylo.dashboard.store import PlanStore
from mylo.ha.registries import EntityEntry, Registries
from mylo.ha.ws_client import CommandError
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _FakeClient:
    """Returns a live config that the save command updates, so read-back
    after save reflects the write (or a tampered version of it)."""

    def __init__(self, config: dict[str, Any], *, save_raises: Exception | None = None, tamper=None) -> None:
        self.config = config
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._save_raises = save_raises
        self._tamper = tamper

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "lovelace/config":
            return copy.deepcopy(self.config)
        if type_ == "lovelace/config/save":
            if self._save_raises is not None:
                raise self._save_raises
            saved = copy.deepcopy(kwargs["config"])
            self.config = self._tamper(saved) if self._tamper else saved
            return None
        return {}

    def saves(self) -> list[dict[str, Any]]:
        return [k["config"] for t, k in self.calls if t == "lovelace/config/save"]


def _registries() -> Registries:
    reg = Registries()
    reg.entities = {
        e: EntityEntry.from_raw({"entity_id": e}) for e in ("light.kitchen", "light.hall")
    }
    return reg


def _dashboard() -> dict[str, Any]:
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


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


async def _staged(tmp_path: Path, client: _FakeClient, store: PlanStore, ops: list[dict[str, Any]]) -> str:
    ctx = make_ctx(ws_client=client, registries=_registries(), tmp_path=tmp_path, plans=store)
    result = await execute("plan_dashboard", {"summary": "s", "operations": ops}, ctx)
    assert result.status.value == "ok", result.data
    return result.data["plan_id"]


def _apply_ctx(tmp_path: Path, client: _FakeClient, store: PlanStore, plan_id: str, **kw: Any):
    return make_ctx(
        ws_client=client,
        registries=_registries(),
        tmp_path=tmp_path,
        plans=store,
        user_approved=kw.pop("user_approved", True),
        approved_plan_ids=kw.pop("approved_plan_ids", frozenset({plan_id})),
        **kw,
    )


OPS = [
    {"op": "add_cards", "view_path": "rooms", "section": "Lights", "cards": [{"type": "tile", "entity": "light.hall"}]},
    {"op": "create_view", "title": "K", "path": "k", "sections": [{"heading": "A", "cards": []}]},
]


async def test_apply_happy_path(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id))
    assert result.status.value == "ok", result.error_message
    data = result.data
    assert data["preview"] is False
    assert data["applied"] == 2
    assert data["verification"]["all_ok"] is True
    assert len(client.saves()) == 1
    saved = client.saves()[0]
    assert saved["views"][0]["sections"][0]["cards"][2]["entity"] == "light.hall"
    assert saved["views"][1]["path"] == "k"
    backup = Path(data["backup"])
    assert backup.exists()
    assert json.loads(backup.read_text()) == _dashboard()
    assert store.get(plan_id) is None


async def test_refuses_unapproved_plan(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    ctx = _apply_ctx(tmp_path, client, store, plan_id, approved_plan_ids=frozenset())
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, ctx)
    assert result.error_code == "plan_not_approved"
    assert client.saves() == []
    assert store.get(plan_id) is not None


async def test_requires_tier2_approval_flag(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    ctx = _apply_ctx(tmp_path, client, store, plan_id, user_approved=False)
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, ctx)
    assert result.error_code == "confirmation_required"


async def test_unknown_or_expired_plan(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    ctx = _apply_ctx(tmp_path, client, store, "zzzz")
    result = await execute("apply_dashboard_plan", {"plan_id": "zzzz"}, ctx)
    assert result.error_code == "plan_not_found"


async def test_other_conversation_plan_is_not_found(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    ctx = _apply_ctx(tmp_path, client, store, plan_id, conversation_id="someone-else")
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, ctx)
    assert result.error_code == "plan_not_found"


async def test_target_changed_aborts_before_save(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard())
    store = PlanStore()
    plan_id = await _staged(
        tmp_path, client, store,
        [{"op": "remove_card", "view_path": "rooms", "section": "Lights", "card_index": 1}],
    )
    # Someone swapped the card between plan and apply.
    client.config["views"][0]["sections"][0]["cards"][1] = {"type": "tile", "entity": "light.hall"}
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id))
    assert result.error_code == "target_changed"
    assert result.data["op_index"] == 0
    assert result.data["actual"]["entity"] == "light.hall"
    assert client.saves() == []


async def test_save_failure_reports_ha_error(tmp_path: Path) -> None:
    client = _FakeClient(_dashboard(), save_raises=CommandError("home_assistant_error", "boom"))
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS)
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id))
    assert result.error_code == "ha_error"
    assert result.data["backup"]


async def test_readback_mismatch_reported(tmp_path: Path) -> None:
    def _drop_new_card(saved: dict[str, Any]) -> dict[str, Any]:
        saved["views"][0]["sections"][0]["cards"].pop()
        return saved

    client = _FakeClient(_dashboard(), tamper=_drop_new_card)
    store = PlanStore()
    plan_id = await _staged(tmp_path, client, store, OPS[:1])
    result = await execute("apply_dashboard_plan", {"plan_id": plan_id}, _apply_ctx(tmp_path, client, store, plan_id))
    assert result.status.value == "ok"
    assert result.data["verification"]["all_ok"] is False
    assert result.data["verification"]["ops"][0]["ok"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_apply_dashboard_plan_tool.py -v`
Expected: FAIL with `unknown_tool`.

- [ ] **Step 3: Implement**

Create `src/mylo/tools/write/apply_dashboard_plan.py`:

```python
"""``apply_dashboard_plan`` — execute a plan the user approved.

The executor's tier-2 gate already requires the turn-wide ``approved``
flag. On top of that the plan id must be in ``approved_plan_ids`` —
the set the UI sends when the user clicks Apply on a plan card — so
the model cannot apply a different plan than the one shown.

All ops are applied in memory, a backup of the pre-apply config is
written, one ``lovelace/config/save`` is issued, and the result is
read back and verified against the plan.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.dashboard.backup import write_backup
from mylo.dashboard.io import (
    DashboardUnavailable,
    fetch_dashboard_config,
    save_dashboard_config,
)
from mylo.dashboard.ops import OpError, OpReceipt, TargetMismatch, apply_op
from mylo.dashboard.verify import verify_plan
from mylo.ha.ws_client import CommandError
from mylo.logging_setup import get_logger
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)


class ApplyDashboardPlanParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(
        min_length=1,
        description="plan_id from plan_dashboard, after the user clicked Apply on it.",
    )


async def handler(params: ApplyDashboardPlanParams, ctx: ToolContext) -> ToolResult:
    if params.plan_id not in ctx.approved_plan_ids:
        return ToolResult.error(
            "plan_not_approved",
            "this plan was not approved by the user — present it and wait for them to click Apply",
        )
    if ctx.plans is None:
        return ToolResult.error("plans_unavailable", "plan store not configured")
    plan = ctx.plans.get(params.plan_id)
    if plan is None or plan.conversation_id != ctx.conversation_id:
        return ToolResult.error(
            "plan_not_found",
            "plan expired or unknown — call plan_dashboard again and re-present it",
        )

    try:
        current = await fetch_dashboard_config(ctx.ws_client, plan.dashboard_id)
    except DashboardUnavailable as exc:
        return ToolResult.error("dashboard_unavailable", f"{exc.code}: {exc.message}")

    work: dict[str, Any] = current
    receipts: list[OpReceipt] = []
    try:
        for op, target in zip(plan.operations, plan.resolved, strict=True):
            work, receipt = apply_op(work, op, target)
            receipts.append(receipt)
    except TargetMismatch as exc:
        return ToolResult.error(
            "target_changed",
            exc.message,
            data={
                "op_index": exc.op_index,
                "expected": exc.expected.model_dump(),
                "actual": exc.actual.model_dump(),
                "hint": "the dashboard changed since the plan was made — query_dashboard and plan again",
            },
        )
    except OpError as exc:
        return ToolResult.error(exc.code, exc.message)

    backup_path: str | None = None
    try:
        backup_path = str(write_backup(ctx.config.mylo_data_dir, plan.dashboard_id, current))
    except OSError as exc:
        log.warning("dashboard.backup_failed", error=str(exc))

    try:
        await save_dashboard_config(ctx.ws_client, plan.dashboard_id, work)
    except CommandError as exc:
        return ToolResult.error(
            "ha_error", f"{exc.code}: {exc.message}", data={"backup": backup_path}
        )

    ctx.plans.remove(plan.plan_id)

    try:
        after = await fetch_dashboard_config(ctx.ws_client, plan.dashboard_id)
        verification = verify_plan(plan, receipts, work, after)
    except DashboardUnavailable as exc:
        verification = {
            "all_ok": False,
            "ops": [],
            "reason": f"could not read the dashboard back: {exc.code}",
        }

    log.info(
        "dashboard.plan_applied",
        plan_id=plan.plan_id,
        ops=len(receipts),
        all_ok=verification.get("all_ok"),
        backup=backup_path,
    )
    return ToolResult.ok(
        {
            "preview": False,
            "plan_id": plan.plan_id,
            "applied": len(receipts),
            "backup": backup_path,
            "verification": verification,
        }
    )


TOOL = ToolDefinition(
    name="apply_dashboard_plan",
    description=(
        "Apply a plan the user approved by clicking Apply on the plan card. "
        "Pass the plan_id from plan_dashboard. Writes a backup, saves once, "
        "reads the dashboard back, and reports per-operation verification. "
        "Refused unless the user's message carried approval for this plan_id."
    ),
    params_model=ApplyDashboardPlanParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
```

Add `"mylo.tools.write.apply_dashboard_plan",` to `_DEFAULT_MODULES` directly after `"mylo.tools.write.modify_dashboard",`.

- [ ] **Step 4: Run, lint, commit**

```bash
pytest tests/unit/test_apply_dashboard_plan_tool.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/tools/write/apply_dashboard_plan.py src/mylo/tools/registry.py tests/unit/test_apply_dashboard_plan_tool.py
git commit -m "feat(tools): apply_dashboard_plan — bound approval, one save, verify

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Remove `modify_dashboard`

**Files:**
- Delete: `src/mylo/tools/write/modify_dashboard.py`, `tests/unit/test_modify_dashboard_sections.py`
- Modify: `src/mylo/tools/registry.py` (drop the module line), `src/mylo/tools/dashboard_refs.py:27-30` (docstring mentions), `src/mylo/tools/read/query_dashboard.py:104-105` (comment), `README.md`, `MYLO_SPEC.md`
- Modify: `tests/unit/test_tools_m7b.py:195-413`, `tests/unit/test_dashboard_schema.py:163-end`, `tests/unit/test_lovelace_meta.py:166-end`

- [ ] **Step 1: Delete the tool and its dedicated test file**

```bash
git rm src/mylo/tools/write/modify_dashboard.py tests/unit/test_modify_dashboard_sections.py
sed -i '' '/"mylo.tools.write.modify_dashboard",/d' src/mylo/tools/registry.py
```

- [ ] **Step 2: Remove the tests that drove the old tool**

- `tests/unit/test_tools_m7b.py`: delete everything from the line `# ─── modify_dashboard ───` (line 195) up to but not including `# ─── rename_entities ───` (line 414). Then run `grep -n "_sections_view_dashboard\|_sections_ctx" tests/unit/test_tools_m7b.py`; if nothing remains, the helpers went with the block. Update the module docstring (lines 15-20) to drop "modify_dashboard".
- `tests/unit/test_dashboard_schema.py`: delete from `# ─── modify_dashboard integration ───` (line 163) to end of file. Remove the now-unused imports (`execute`, `make_ctx`, `tool_registry`, `Registries`, any fake client) that ruff reports.
- `tests/unit/test_lovelace_meta.py`: delete from `# ─── modify_dashboard theme validation ───` (line 166) to end of file. The equivalent coverage is `test_theme_checked_when_listable` / `test_theme_skipped_when_unavailable` in Task 9. Remove unused imports ruff reports.

- [ ] **Step 3: Update prose references**

```bash
grep -rn "modify_dashboard" src README.md MYLO_SPEC.md
```

- `src/mylo/tools/dashboard_refs.py`: change "Used by :mod:`modify_dashboard` as a pre-flight check before the dry-run preview" to "Used by :mod:`mylo.dashboard.validate` before a plan is staged".
- `src/mylo/tools/read/query_dashboard.py`: the comment about `section_index` is rewritten in Task 12; leave for now.
- `README.md`: replace each `modify_dashboard` mention with `plan_dashboard` / `apply_dashboard_plan` and one sentence: "Dashboard changes are planned, shown as a wireframe for approval, then applied and verified."
- `MYLO_SPEC.md`: at the `modify_dashboard` tool definition (around line 769), replace the block with a pointer: "Superseded by `plan_dashboard` / `apply_dashboard_plan` — see `docs/superpowers/specs/2026-09-19-dashboard-plan-flow-design.md`."

- [ ] **Step 4: Run, lint, commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
python -c "from mylo.server.app import build_app; print('import ok')"
git add -A
git commit -m "refactor(tools)!: remove modify_dashboard in favour of plan/apply

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: `query_dashboard` section summaries and `verify_change` fixes

**Files:**
- Modify: `src/mylo/tools/read/query_dashboard.py:93-113`
- Modify: `src/mylo/tools/read/verify_change.py:136-146`, `:153-157`
- Test: new `tests/unit/test_query_dashboard_summary.py`; `tests/unit/test_verify_dashboard_loaded.py:134-150`

**Interfaces:**
- Produces: `_view_summary` output gains `sections: [{index, heading, cards: [{index, type, entity?, heading?}]}]` for sections views and `cards: [...]` for masonry.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_query_dashboard_summary.py`:

```python
from __future__ import annotations

from mylo.tools.read.query_dashboard import _view_summary


def test_sections_view_lists_headings_and_card_fingerprints() -> None:
    view = {
        "path": "rooms",
        "title": "Rooms",
        "type": "sections",
        "sections": [
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Lights"},
                    {"type": "tile", "entity": "light.a"},
                    {"type": "entities", "entities": [{"entity": "sensor.t"}]},
                ],
            },
            {"type": "grid", "cards": [{"type": "markdown", "content": "x"}]},
        ],
    }
    s = _view_summary(view)
    assert s["layout"] == "sections"
    assert s["section_count"] == 2
    assert s["sections"][0]["heading"] == "Lights"
    assert s["sections"][1]["heading"] is None
    assert s["sections"][0]["cards"] == [
        {"index": 0, "type": "heading", "heading": "Lights"},
        {"index": 1, "type": "tile", "entity": "light.a"},
        {"index": 2, "type": "entities", "entity": "sensor.t"},
    ]


def test_masonry_view_lists_cards() -> None:
    s = _view_summary({"path": "home", "cards": [{"type": "tile", "entity": "light.a"}]})
    assert "layout" not in s
    assert s["cards"] == [{"index": 0, "type": "tile", "entity": "light.a"}]
```

In `tests/unit/test_verify_dashboard_loaded.py`, replace `test_dashboard_loaded_sections_view_missing_cards_flagged` with:

```python
async def test_dashboard_loaded_section_without_cards_is_legal(tmp_path):
    """A heading-only or empty section has no 'cards' key in HA and is valid."""
    config = {
        "views": [
            {"path": "rooms", "type": "sections", "sections": [{"type": "grid"}, {"type": "grid", "cards": []}]}
        ]
    }
    client = _FakeClient({"lovelace/config": config})
    ctx = make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path)
    result = await execute(
        "verify_change", {"check_type": "dashboard_loaded", "targets": ["rooms"], "wait_seconds": 0}, ctx
    )
    entry = result.data["results"]["rooms"]
    assert entry["ok"] is True
    assert entry["section_count"] == 2


async def test_dashboard_loaded_non_dict_section_is_malformed(tmp_path):
    config = {"views": [{"path": "rooms", "type": "sections", "sections": ["nope"]}]}
    client = _FakeClient({"lovelace/config": config})
    ctx = make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path)
    result = await execute(
        "verify_change", {"check_type": "dashboard_loaded", "targets": ["rooms"], "wait_seconds": 0}, ctx
    )
    assert result.data["results"]["rooms"]["ok"] is False


async def test_dashboard_loaded_does_not_sleep(tmp_path, monkeypatch):
    import asyncio as _asyncio

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)
    client = _FakeClient({"lovelace/config": _DEFAULT_DASHBOARD})
    ctx = make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path)
    await execute("verify_change", {"check_type": "dashboard_loaded", "targets": ["home"]}, ctx)
    assert slept == []
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_query_dashboard_summary.py tests/unit/test_verify_dashboard_loaded.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement `query_dashboard`**

In `src/mylo/tools/read/query_dashboard.py` add the import `from mylo.dashboard.plan import card_fingerprint, is_heading_card` and `from mylo.dashboard.ops import section_heading`, then replace `_view_summary` with:

```python
def _card_summary(index: int, card: Any) -> dict[str, Any]:
    fp = card_fingerprint(card)
    out: dict[str, Any] = {"index": index, "type": fp.type}
    if fp.entity:
        out["entity"] = fp.entity
    if is_heading_card(card):
        out["heading"] = card.get("heading")
    return out


def _view_summary(view: dict[str, Any]) -> dict[str, Any]:
    cards = view.get("cards") or []
    sections = view.get("sections")
    summary: dict[str, Any] = {
        "path": view.get("path"),
        "title": view.get("title"),
        "icon": view.get("icon"),
        "card_count": len(cards) if isinstance(cards, list) else 0,
    }
    # Sections views carry per-section headings and per-card {index,
    # type, entity} so plan_dashboard ops can address a section by
    # heading and a card by index without a second query.
    if view.get("type") == "sections" or isinstance(sections, list):
        summary["layout"] = "sections"
        if isinstance(sections, list):
            summary["section_count"] = len(sections)
            summary["sections"] = [
                {
                    "index": i,
                    "heading": section_heading(s),
                    "cards": [
                        _card_summary(j, c)
                        for j, c in enumerate(s.get("cards") or [] if isinstance(s, dict) else [])
                    ],
                }
                for i, s in enumerate(sections)
            ]
    elif isinstance(cards, list):
        summary["cards"] = [_card_summary(j, c) for j, c in enumerate(cards)]
    return summary
```

Drop the now-redundant `section_card_counts` key if any test asserted it (`grep -rn section_card_counts tests`); update that test to use `sections[i].cards` length instead.

- [ ] **Step 4: Implement `verify_change` fixes**

In `src/mylo/tools/read/verify_change.py`, replace the sections branch inside `_check_dashboard_loaded` (`if is_sections:` … `else:` block that sets `card_count`) with:

```python
        if is_sections:
            sections = view.get("sections")
            if isinstance(sections, list) and all(isinstance(s, dict) for s in sections):
                entry["section_count"] = len(sections)
                entry["card_count"] = sum(len(s.get("cards") or []) for s in sections)
            else:
                entry["ok"] = False
                entry["reason"] = "sections view has malformed sections (non-mapping entry)"
        else:
            entry["card_count"] = len(view.get("cards") or [])
```

And in `handler`, change the sleep guard to:

```python
    # Lovelace saves apply synchronously; only reload-based checks need
    # the grace period.
    if params.wait_seconds > 0 and params.check_type != "dashboard_loaded":
        await asyncio.sleep(params.wait_seconds)
```

Update the tool description string: replace "checks the view exists and its sections are well-formed" with "checks the view exists; applies immediately, no wait". Also update the `wait_seconds` field description to append "Ignored for dashboard_loaded."

- [ ] **Step 5: Run, lint, commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/tools/read/query_dashboard.py src/mylo/tools/read/verify_change.py tests/unit/test_query_dashboard_summary.py tests/unit/test_verify_dashboard_loaded.py
git commit -m "feat(tools): query_dashboard lists headings and card indices; dashboard_loaded no longer sleeps

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: Prompt 0.6.0 and reference examples

**Files:**
- Modify: `src/mylo/data/system_prompt.txt:1-12` (header), `:25-27` (tier-2 tool list), `:57-82` (entity rules line 74), `:84-115` (dashboard block)
- Modify: `src/mylo/data/PROMPT_CHANGELOG.md` (new entry at top)
- Rewrite: `src/mylo/data/references/dashboard_examples.yaml`

- [ ] **Step 1: Bump the header**

Change line 1 to `# version: 0.6.0` and insert after line 5:

```
# v0.6.0 adds: plan_dashboard / apply_dashboard_plan flow (plan, wait for
# Apply, apply+verify), section addressing by heading, positional
# placement, grid_options sizing; removes modify_dashboard + dry_run for
# dashboards.
```

- [ ] **Step 2: Update the write-flow list**

In the "Write flow (CRITICAL)" block, after the bullet ending `modify the user's Home Assistant configuration. NEVER call them with dry_run=false as the first call for a change.` add:

```
- Dashboards are the exception: they use plan_dashboard (stage) and
  apply_dashboard_plan (execute) instead of dry_run. See "Dashboard
  work" below.
```

In the entity-ID rules, change the line starting `- If modify_dashboard returns invalid_entity_refs, fix EVERY listed` to `- If plan_dashboard returns invalid_entity_refs issues, fix EVERY listed`.

- [ ] **Step 3: Replace the dashboard block**

Replace everything from `Dashboard design (CRITICAL — this is what makes dashboards look good):` through `fall back to markdown headings.` with:

```
Dashboard work (CRITICAL — plan, wait, apply):
- Dashboards do NOT use dry_run. The flow is: gather → ask once →
  plan_dashboard → STOP → apply_dashboard_plan on the approval turn.
- Gather: call query_dashboard_env ONCE (installed themes + which
  custom:* cards exist — never guess a theme, never use a custom card
  that isn't listed; when the list is null use native cards only).
  Get entities in ONE broad query_entities call. When editing an
  existing view, query_dashboard with dashboard_id + view_id shows
  section headings and card indices to address.
- Ask: when theme, style (minimal vs data-dense), or scope (which
  areas) is genuinely open and stored preferences don't answer it,
  ask ONE consolidated ask_user question (2-4 options) BEFORE
  planning. Record lasting answers with memory_note(type="preference").
- Plan: ONE plan_dashboard call holding every operation for the
  change. Put every choice you made without asking in `assumptions`.
  If it returns plan_invalid, fix every listed issue and plan again —
  never ask the user to approve a known-bad plan.
- Wait: after a successful plan_dashboard, say the plan is ready to
  review and END YOUR TURN. The user clicks Apply or Modify. Never
  call apply_dashboard_plan in the same turn as plan_dashboard.
- Apply: when the user's message approves, call
  apply_dashboard_plan(plan_id) and report the verification result.
  If all_ok is false, say which operation failed and where the backup
  is. Do not call verify_change for dashboards — apply verifies.
- Design: sections layout (layout="masonry" only if the user asks).
  Every section has a `heading`; the heading card is added for you —
  never add mushroom-title-card or markdown titles. Group by area OR
  by function, one scheme per view. 4-8 cards per section; split
  bigger groups. Order sections by daily use, diagnostics last.
  `tile` is the default single-entity card; `entities` for dense
  readouts.
- Sizing: per-card width is `grid_options: {columns: N}` ON THE CARD
  (12 columns per section; "full" spans the row). Use it for graphs,
  maps, weather. `column_span` on a section widens the WHOLE section
  and is only for a section holding one big card.
- Placement: address sections by heading text ("Climate") or index.
  Use `position` ("start", "end", or an index) to put a card exactly
  where the user asked. Use move_card to relocate; never remove + add.
- Do not silently convert an existing masonry view to sections — ask
  first via ask_user.
```

- [ ] **Step 4: Prompt changelog**

Insert at the top of `src/mylo/data/PROMPT_CHANGELOG.md` (after the intro paragraph):

```markdown
## 0.6.0 — 2026-09-19

Dashboard plan flow. `modify_dashboard` and its dry_run dance are gone;
dashboards now go plan → wait → apply:

- "Dashboard work" section rewritten around plan_dashboard /
  apply_dashboard_plan: one plan call with every operation, list
  unasked choices in `assumptions`, end the turn after planning, apply
  only on the approval turn, report the built-in verification.
- Sizing corrected: per-card width is `grid_options.columns`; section
  `column_span` widens the whole section.
- Placement vocabulary: sections by heading text, `position`
  start/end/index, move_card instead of remove + add.
- Write-flow block notes dashboards are the dry_run exception.
```

- [ ] **Step 5: Rewrite the examples file**

Replace `src/mylo/data/references/dashboard_examples.yaml` with:

```yaml
# Dashboard reference. Mylo uses these as few-shot examples when
# planning dashboards. The first block is a complete plan_dashboard
# call; the rest are card snippets to drop into `cards` lists. This
# comment block IS shown to the model.

# ─── A complete plan_dashboard call for a new room view ─────────────
# One create_view with one section per group. Headings are added by
# the tool from `heading` — never put a heading card in `cards`.
# `assumptions` lists what was decided without asking.
summary: "Create Kitchen view — Lights, Climate, History"
assumptions:
  - "Included only entities in the Kitchen area"
  - "Default theme (none stored)"
operations:
  - op: create_view
    title: Kitchen
    path: kitchen
    icon: mdi:silverware-fork-knife
    max_columns: 4
    sections:
      - heading: Lights
        cards:
          - type: tile
            entity: light.kitchen_overhead
          - type: tile
            entity: light.kitchen_pendant
      - heading: Climate
        cards:
          - type: thermostat
            entity: climate.kitchen
            grid_options:
              columns: full
          - type: tile
            entity: sensor.kitchen_temperature
          - type: tile
            entity: sensor.kitchen_humidity
      - heading: History
        column_span: 2          # widens the WHOLE section (one big card)
        cards:
          - type: history-graph
            entities:
              - sensor.kitchen_temperature
            hours_to_show: 24

# ─── Editing an existing view: address by heading, place by position ─
# From query_dashboard: view "rooms" has sections "Lights" (index 0)
# and "Climate" (index 1); Climate's cards are [heading, tile, tile].
operations:
  - op: add_cards
    view_path: rooms
    section: Climate
    position: start            # lands right after the heading card
    cards:
      - type: thermostat
        entity: climate.main
        grid_options:
          columns: 12          # full width of the section
  - op: move_card
    view_path: rooms
    from_section: Lights
    card_index: 3
    to_section: Climate
    position: end
  - op: replace_card
    view_path: rooms
    section: Lights
    card_index: 1              # index 0 is the heading
    card:
      type: light
      entity: light.kitchen_overhead
  - op: update_view_meta
    view_path: rooms
    title: Rooms & Comfort
    icon: mdi:sofa

# ─── Energy section (native cards) ──────────────────────────────────
- heading: Energy
  column_span: 2
  cards:
    - type: energy-date-selection
    - type: energy-usage-graph
    - type: energy-distribution

# ─── Native building blocks ─────────────────────────────────────────

# Entities card — dense readout when tiles would be too sparse.
- type: entities
  title: Diagnostics
  entities:
    - entity: sensor.kitchen_temperature
    - entity: binary_sensor.kitchen_window
      secondary_info: last-changed

# Conditional card — only shows when something needs attention.
- type: conditional
  conditions:
    - condition: state
      entity: cover.garage_door
      state: open
  card:
    type: tile
    entity: cover.garage_door
    color: red

# Wide cards: size the CARD with grid_options, not the section.
- type: map
  entities:
    - person.max
  grid_options:
    columns: full
    rows: 4
- type: weather-forecast
  entity: weather.home
  grid_options:
    columns: 8

# Markdown — free text.
- type: markdown
  content: "## Notes\nBoiler service due in March."

# ─── Custom cards — ONLY if query_dashboard_env lists them ──────────
- type: custom:mushroom-light-card
  entity: light.living_room
  show_brightness_control: true
- type: custom:mini-graph-card
  entities:
    - entity: sensor.basement_humidity
  hours_to_show: 24
  line_width: 2
```

The file is read as text by `context/references.py`, not parsed as YAML, so the multiple top-level documents are fine. Confirm with `grep -n "yaml" src/mylo/context/references.py`; if it parses, wrap each block as a comment-separated string the same way the current file does.

- [ ] **Step 6: Run the prompt-related tests, lint, commit**

```bash
pytest tests/unit -q -k "prompt or assembler or references or task_detector"
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/data/system_prompt.txt src/mylo/data/PROMPT_CHANGELOG.md src/mylo/data/references/dashboard_examples.yaml
git commit -m "feat(prompt): dashboard plan flow rules v0.6.0 + grid_options sizing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: UI plumbing — approval carries plan ids, question answers do not

**Files:**
- Modify: `ui/src/api.ts:39-63`
- Modify: `ui/src/types.ts` (append plan types)
- Modify: `ui/src/components/Composer.tsx`
- Modify: `ui/src/App.tsx`

**Interfaces:**
- Produces: `SendOptions.approvedPlanIds?: string[]` → body `approved_plan_ids`; `DashboardPlanData` and friends in `types.ts`; `Composer` prop `draft?: { text: string; nonce: number } | null`; `handleSubmit(message, opts?)`; `ApprovalContext.plan?: DashboardPlanData`; exported helpers `planIdsFromRecords`, `planIdsFromItems` in `App.tsx`.

- [ ] **Step 1: api.ts**

```ts
export interface SendOptions {
  approved?: boolean;
  approvedPlanIds?: string[];
  sessionCostUsd?: number;
  signal?: AbortSignal;
}
```

and in the fetch body:

```ts
    body: JSON.stringify({
      message,
      approved: Boolean(options.approved),
      approved_plan_ids: options.approvedPlanIds ?? [],
      session_cost_usd: options.sessionCostUsd ?? 0,
    }),
```

- [ ] **Step 2: types.ts**

Append:

```ts
// ─── Dashboard plan (plan_dashboard result) ────────────────────────────────

export interface PlanSectionData {
  heading: string;
  cards: Record<string, unknown>[];
  column_span?: number | null;
}

// Ops are rendered by their `op` tag; other fields are read loosely.
export interface PlanOpData {
  op: string;
  [key: string]: unknown;
}

export interface PlanFingerprint {
  type: string;
  entity?: string | null;
}

export interface PlanResolvedTarget {
  op_index: number;
  section_index?: number | null;
  to_section_index?: number | null;
  card_index?: number | null;
  fingerprint?: PlanFingerprint | null;
}

export interface PlanIssueData {
  severity: "error" | "warning";
  code: string;
  message: string;
  op_index?: number | null;
}

export interface DashboardPlanData {
  plan_id: string;
  dashboard_id: string | null;
  summary: string;
  assumptions: string[];
  operations: PlanOpData[];
  resolved: PlanResolvedTarget[];
  issues: PlanIssueData[];
}
```

- [ ] **Step 3: Composer draft prop**

In `ui/src/components/Composer.tsx`:

```ts
import { useEffect, useRef, useState } from "react";
```

```ts
interface Props {
  disabled?: boolean;
  onSubmit: (message: string) => void | Promise<void>;
  // Set by the plan card's Modify button: pre-fill and focus. The
  // nonce changes on every request so the same text can be re-applied.
  draft?: { text: string; nonce: number } | null;
}
```

```ts
export function Composer({ disabled, onSubmit, draft }: Props) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const lastContext = useSession((s) => s.lastContextTokens);
  const cost = useSession((s) => s.costUsd);

  useEffect(() => {
    if (!draft) return;
    setText(draft.text);
    const el = textareaRef.current;
    if (el) {
      el.focus();
      el.setSelectionRange(draft.text.length, draft.text.length);
    }
  }, [draft]);
```

Rest unchanged.

- [ ] **Step 4: App.tsx state and handlers**

Imports: add `DashboardPlanCard` and the plan type:

```ts
import { DashboardPlanCard } from "./components/DashboardPlanCard";
import type { ChatFragment, ChatItem, DashboardPlanData, ToolCallRecord } from "./types";
```

(`DashboardPlanCard` is created in Task 15; until then, type-check will fail on this import — do Tasks 14 and 15 in one sitting and commit them together, or create a placeholder component in Task 14 that Task 15 fills in. Preferred: create `DashboardPlanCard.tsx` in this task with only the props interface and a `return null;` body, then replace it in Task 15.)

State changes:

```ts
  // Set when the last turn included a previewed write. planIds are the
  // plan_dashboard ids in that turn; Apply sends them back so the
  // server can bind approval to exactly those plans.
  const [pendingApproval, setPendingApproval] = useState<{ planIds: string[] } | null>(null);
  const [queuedApply, setQueuedApply] = useState<{ message: string; planIds: string[] } | null>(null);
  const [composerDraft, setComposerDraft] = useState<{ text: string; nonce: number } | null>(null);
```

`handleNewConversation`: `setPendingApproval(null);`.

`handleSubmit` — new signature and body changes:

```ts
  interface SubmitOptions {
    approved?: boolean;
    approvedPlanIds?: string[];
  }

  const handleSubmit = useCallback(
    async (message: string, opts: SubmitOptions = {}) => {
      // ...slash-command handling unchanged...

      setError(null);
      setCatchup(null);
      // Approval comes ONLY from the Apply button. Answering a question
      // card or typing a message never authorizes a write.
      const approved = Boolean(opts.approved);
      const approvedPlanIds = opts.approvedPlanIds ?? [];
      setPendingApproval(null);
      // ...userId/assistantId/setItems/setSending unchanged...

      try {
        for await (const event of streamChat(message, {
          approved,
          approvedPlanIds,
          sessionCostUsd: sessionCost,
        })) {
          // unchanged
        }
      } catch (exc) {
        // unchanged
      } finally {
        setItems((prev) =>
          prev.map((it) => (it.id === assistantId ? { ...it, pending: false } : it)),
        );
        setSending(false);
        if (turnSawPreview) {
          setPendingApproval({ planIds: planIdsFromRecords(toolCallsById.values()) });
        }
      }
    },
    [handleNewConversation, recordTurn, sessionCost],
  );
```

Move `SubmitOptions` to module scope (above `App`). Note `sessionCost` was already read inside the callback; add it to the dependency list as shown.

Hydration effect: replace `setPendingApproval(true)` with `setPendingApproval({ planIds: planIdsFromItems(hydrated) })`.

`handleApply`:

```ts
  const handleApply = useCallback(async () => {
    const message =
      approvalCount > 1
        ? `Yes, apply all ${approvalCount} changes.`
        : "Yes, apply the change.";
    const planIds = pendingApproval?.planIds ?? [];
    if (sending) {
      setQueuedApply({ message, planIds });
      return;
    }
    await handleSubmit(message, { approved: true, approvedPlanIds: planIds });
  }, [handleSubmit, sending, approvalCount, pendingApproval]);

  const handleReject = useCallback(() => {
    setPendingApproval(null);
    setQueuedApply(null);
  }, []);

  const handleModify = useCallback(() => {
    setComposerDraft({ text: "Change the plan: ", nonce: Date.now() });
  }, []);
```

Queued flush:

```ts
  useEffect(() => {
    if (!sending && queuedApply) {
      const q = queuedApply;
      setQueuedApply(null);
      void handleSubmit(q.message, { approved: true, approvedPlanIds: q.planIds });
    }
  }, [sending, queuedApply, handleSubmit]);
```

Render — replace the `QuestionCard` and `ApprovalCard` block with:

```tsx
            {pendingQuestion && !sending ? (
              <div style={{ paddingRight: 40 }}>
                <QuestionCard
                  question={pendingQuestion}
                  onSelect={(label) => void handleSubmit(label)}
                  disabled={sending}
                />
              </div>
            ) : null}
            {pendingApproval && approvalCount > 0 ? (
              <div style={{ paddingRight: 40 }}>
                {planContexts.length > 0 ? (
                  <DashboardPlanCard
                    plans={planContexts.map((c) => c.plan!)}
                    otherChanges={otherContexts.map((c) => c.description)}
                    onApprove={() => void handleApply()}
                    onReject={handleReject}
                    onModify={handleModify}
                    applying={queuedApply !== null}
                  />
                ) : (
                  <ApprovalCard
                    items={approvalContexts}
                    onApprove={() => void handleApply()}
                    onReject={handleReject}
                    applying={queuedApply !== null}
                  />
                )}
              </div>
            ) : null}
```

with, next to `approvalContexts`:

```ts
  const planContexts = approvalContexts.filter((c) => c.plan !== undefined);
  const otherContexts = approvalContexts.filter((c) => c.plan === undefined);
```

Composer: `<Composer disabled={sending} onSubmit={(m) => handleSubmit(m)} draft={composerDraft} />`.

`ApprovalContext` gains `plan?: DashboardPlanData;`. In `buildApprovalContext`, before the `modify_dashboard` branch (which you now delete, since the tool is gone), add:

```ts
  // Dashboard plan — rendered by DashboardPlanCard, not the generic card.
  if (call.name === "plan_dashboard" && data.plan && typeof data.plan === "object") {
    const plan = data.plan as DashboardPlanData;
    return { description: plan.summary, plan, tierLabel: "TIER-2" };
  }
```

Helpers at module scope:

```ts
export function planIdsFromRecords(records: Iterable<ToolCallRecord>): string[] {
  const ids: string[] = [];
  for (const call of records) {
    if (call.name !== "plan_dashboard" || call.state !== "ok") continue;
    const data = call.data as Record<string, unknown> | undefined;
    if (data?.preview === true && typeof data.plan_id === "string") ids.push(data.plan_id);
  }
  return ids;
}

export function planIdsFromItems(items: ChatItem[]): string[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.role === "user") return [];
    if (item.role !== "assistant") continue;
    const records = item.fragments.flatMap((f) => (f.kind === "tool" ? [f.call] : []));
    return planIdsFromRecords(records);
  }
  return [];
}
```

- [ ] **Step 5: Type-check and commit**

```bash
cd ui && npx tsc -b --noEmit && cd ..
git add ui/src/api.ts ui/src/types.ts ui/src/components/Composer.tsx ui/src/App.tsx ui/src/components/DashboardPlanCard.tsx
git commit -m "feat(ui): approval carries plan ids; question answers never approve

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 15: `DashboardPlanCard`

**Files:**
- Create/replace: `ui/src/components/DashboardPlanCard.tsx`

**Interfaces:**
- Consumes: `DashboardPlanData`, `PlanOpData`, `PlanSectionData`, `PlanResolvedTarget` from `types.ts`; `StatusDot`, `Tag` components.
- Produces: `DashboardPlanCard({ plans, otherChanges, onApprove, onReject, onModify, applying? })`.

- [ ] **Step 1: Write the component**

```tsx
// (license header)

import { useState } from "react";
import type {
  DashboardPlanData,
  PlanFingerprint,
  PlanOpData,
  PlanSectionData,
} from "../types";
import { StatusDot } from "./StatusDot";
import { Tag } from "./Tag";

interface Props {
  plans: DashboardPlanData[];
  // Non-dashboard previews in the same turn, described in one line each.
  otherChanges: string[];
  onApprove: () => void;
  onReject: () => void;
  onModify: () => void;
  applying?: boolean;
}

// Wireframe preview of a dashboard plan: one block per operation, each
// section drawn as a box with its heading and card chips, then the
// assumptions the model made and any lint warnings. Apply sends the
// plan ids back; Modify pre-fills the composer.
export function DashboardPlanCard({
  plans,
  otherChanges,
  onApprove,
  onReject,
  onModify,
  applying = false,
}: Props) {
  const [showYaml, setShowYaml] = useState(false);
  const assumptions = plans.flatMap((p) => p.assumptions);
  const warnings = plans.flatMap((p) => p.issues.filter((i) => i.severity === "warning"));

  return (
    <div
      className="rounded border"
      style={{ borderColor: "var(--color-border-accent)", borderWidth: 1 }}
    >
      <div
        className="flex items-center gap-2 px-3 py-2 border-b"
        style={{ borderColor: "var(--color-border)" }}
      >
        <StatusDot tone="accent" pulse />
        <span
          className="font-mono text-[10px] font-bold uppercase tracking-label"
          style={{ color: "var(--color-accent)" }}
        >
          Dashboard plan
        </span>
        <span className="font-sans text-[12px]" style={{ color: "var(--color-text)" }}>
          {plans.map((p) => p.summary).join(" · ")}
        </span>
      </div>

      {plans.map((plan) => (
        <div key={plan.plan_id}>
          {plan.operations.map((op, i) => (
            <OpBlock
              key={`${plan.plan_id}-${i}`}
              op={op}
              index={i}
              fingerprint={plan.resolved.find((r) => r.op_index === i)?.fingerprint ?? null}
              dashboardId={plan.dashboard_id}
            />
          ))}
        </div>
      ))}

      {otherChanges.map((text, i) => (
        <div
          key={`other-${i}`}
          className="px-3 py-2 font-sans text-[12px] border-t"
          style={{ borderColor: "var(--color-border)", color: "var(--color-text)" }}
        >
          <Tag tone="muted">TIER-2</Tag> <span className="ml-2">{text}</span>
        </div>
      ))}

      {assumptions.length > 0 ? (
        <div className="px-3 py-2 border-t" style={{ borderColor: "var(--color-border)" }}>
          <div
            className="font-mono text-[10px] uppercase tracking-label mb-1"
            style={{ color: "var(--color-text-dim)" }}
          >
            Mylo assumed
          </div>
          <ul className="font-sans text-[12px] list-disc pl-4" style={{ color: "var(--color-text)" }}>
            {assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {warnings.length > 0 ? (
        <div className="px-3 py-2 border-t" style={{ borderColor: "var(--color-border)" }}>
          <div
            className="font-mono text-[10px] uppercase tracking-label mb-1"
            style={{ color: "var(--color-warning)" }}
          >
            Warnings
          </div>
          {warnings.map((w, i) => (
            <div key={i} className="font-mono text-[10px]" style={{ color: "var(--color-text-muted)" }}>
              {w.code}: {w.message}
            </div>
          ))}
        </div>
      ) : null}

      {showYaml ? (
        <pre
          className="px-3 py-2 border-t font-mono text-[10px] overflow-x-auto"
          style={{ borderColor: "var(--color-border)", color: "var(--color-text-muted)" }}
        >
          {plans.map((p) => toYaml(p.operations)).join("\n---\n")}
        </pre>
      ) : null}

      <div
        className="flex items-center justify-end gap-2 px-3 py-2 border-t"
        style={{ borderColor: "var(--color-border)" }}
      >
        <button type="button" onClick={onReject} className={ghostBtn} style={ghostStyle}>
          Reject
        </button>
        <button type="button" onClick={onModify} className={ghostBtn} style={ghostStyle}>
          Modify
        </button>
        <button
          type="button"
          onClick={() => setShowYaml((v) => !v)}
          className={ghostBtn}
          style={ghostStyle}
        >
          {showYaml ? "Hide YAML" : "Show YAML"}
        </button>
        <button
          type="button"
          onClick={onApprove}
          disabled={applying}
          className="btn-glow rounded px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label hover:brightness-110 disabled:opacity-60"
          style={{
            backgroundColor: "var(--color-accent-soft)",
            border: "1px solid var(--color-accent)",
            color: "var(--color-accent)",
          }}
        >
          {applying ? "Applying…" : "Apply"}
        </button>
      </div>
    </div>
  );
}

const ghostBtn =
  "rounded border px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label hover:opacity-80";
const ghostStyle = {
  borderColor: "var(--color-border)",
  color: "var(--color-text-muted)",
  background: "transparent",
} as const;

// ─── Operation blocks ────────────────────────────────────────────────────────

function OpBlock({
  op,
  index,
  fingerprint,
  dashboardId,
}: {
  op: PlanOpData;
  index: number;
  fingerprint: PlanFingerprint | null;
  dashboardId: string | null;
}) {
  const view = str(op.view_path ?? op.path);
  const where = dashboardId ? `${dashboardId}/${view}` : view;
  const sectionOf = (key: string) => {
    const v = op[key];
    return v === undefined || v === null ? null : typeof v === "number" ? `section ${v}` : `"${str(v)}"`;
  };
  const target = fingerprint
    ? `${fingerprint.type}${fingerprint.entity ? ` · ${fingerprint.entity}` : ""}`
    : `card #${str(op.card_index)}`;
  const destructive = op.op === "remove_card" || op.op === "remove_section" || op.op === "delete_view";

  let title: string;
  let body: React.ReactNode = null;
  switch (op.op) {
    case "create_view": {
      const sections = (op.sections as PlanSectionData[] | undefined) ?? [];
      const layout = str(op.layout ?? "sections");
      title = `Create view "${str(op.title)}" (/${view}) · ${layout}${
        layout === "sections" ? ` · ${sections.length} section${sections.length === 1 ? "" : "s"}` : ""
      }${positionSuffix(op.position, "view")}`;
      body =
        layout === "sections" ? (
          <div className="space-y-1.5">
            {sections.map((s, i) => (
              <SectionBox key={i} section={s} />
            ))}
          </div>
        ) : (
          <CardChips cards={(op.cards as Record<string, unknown>[] | undefined) ?? []} />
        );
      break;
    }
    case "add_section": {
      const section = op.section as PlanSectionData;
      title = `Add section "${section.heading}" to ${where}${positionSuffix(op.position, "section")}`;
      body = <SectionBox section={section} />;
      break;
    }
    case "add_cards": {
      const cards = (op.cards as Record<string, unknown>[] | undefined) ?? [];
      title = `Add ${cards.length} card${cards.length === 1 ? "" : "s"} to ${where}${
        sectionOf("section") ? ` › ${sectionOf("section")}` : ""
      }${positionSuffix(op.position, "card")}`;
      body = <CardChips cards={cards} />;
      break;
    }
    case "replace_card": {
      const card = op.card as Record<string, unknown>;
      title = `Replace ${target} in ${where}${sectionOf("section") ? ` › ${sectionOf("section")}` : ""} with ${chipLabel(card)}`;
      break;
    }
    case "remove_card":
      title = `Remove ${target} from ${where}${sectionOf("section") ? ` › ${sectionOf("section")}` : ""}`;
      break;
    case "move_card":
      title = `Move ${target} from ${where}${sectionOf("from_section") ? ` › ${sectionOf("from_section")}` : ""} to ${
        sectionOf("to_section") ?? sectionOf("from_section") ?? "same section"
      }${positionSuffix(op.position, "card")}`;
      break;
    case "update_view_meta": {
      const changes = ["title", "icon", "theme", "max_columns", "new_path"]
        .filter((k) => op[k] !== undefined && op[k] !== null)
        .map((k) => `${k} → ${str(op[k])}`);
      title = `Update view ${where}: ${changes.join(", ") || "no changes"}`;
      break;
    }
    case "remove_section":
      title = `Remove section ${sectionOf("section")} from ${where}`;
      break;
    case "delete_view":
      title = `Delete view ${where}`;
      break;
    default:
      title = `${op.op} on ${where}`;
  }

  return (
    <div
      className="px-3 py-3 space-y-2"
      style={index > 0 ? { borderTop: "1px solid var(--color-border)" } : undefined}
    >
      <div className="flex items-start gap-2">
        <span className="font-mono text-[10px] pt-0.5" style={{ color: "var(--color-text-dim)" }}>
          {index + 1}.
        </span>
        <span
          className="font-sans text-[13px]"
          style={{ color: destructive ? "var(--color-warning)" : "var(--color-text)" }}
        >
          {title}
        </span>
      </div>
      {body ? <div className="pl-5">{body}</div> : null}
    </div>
  );
}

function SectionBox({ section }: { section: PlanSectionData }) {
  return (
    <div
      className="rounded border px-2 py-1.5"
      style={{ borderColor: "var(--color-border)", backgroundColor: "var(--color-surface)" }}
    >
      <div
        className="font-mono text-[10px] font-bold uppercase tracking-label mb-1"
        style={{ color: "var(--color-text-muted)" }}
      >
        {section.heading}
        {section.column_span ? ` · spans ${section.column_span}` : ""}
      </div>
      <CardChips cards={section.cards} />
    </div>
  );
}

function CardChips({ cards }: { cards: Record<string, unknown>[] }) {
  if (cards.length === 0) {
    return (
      <div className="font-mono text-[10px]" style={{ color: "var(--color-text-dim)" }}>
        (no cards)
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-1">
      {cards.map((card, i) => (
        <span
          key={i}
          className="rounded border px-1.5 py-0.5 font-mono text-[10px]"
          style={{
            borderColor: "var(--color-border)",
            color: "var(--color-text)",
            flexBasis: isWide(card) ? "100%" : undefined,
          }}
        >
          {chipLabel(card)}
        </span>
      ))}
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function str(v: unknown): string {
  return v === undefined || v === null ? "" : String(v);
}

function positionSuffix(position: unknown, noun: string): string {
  if (position === undefined || position === null || position === "end") return "";
  if (position === "start") return ` at the start`;
  return ` at ${noun} position ${str(position)}`;
}

function chipLabel(card: Record<string, unknown>): string {
  const type = str(card.type) || "?";
  if (type === "heading") return `heading · ${str(card.heading)}`;
  const entity = card.entity;
  if (typeof entity === "string") return `${type} · ${entity}`;
  const entities = card.entities;
  if (Array.isArray(entities) && entities.length > 0) {
    const first = entities[0];
    const id = typeof first === "string" ? first : str((first as Record<string, unknown>)?.entity);
    return `${type} · ${id}${entities.length > 1 ? ` +${entities.length - 1}` : ""}`;
  }
  if (type === "markdown") return `markdown · ${str(card.content).slice(0, 24)}`;
  return type;
}

function isWide(card: Record<string, unknown>): boolean {
  const opts = card.grid_options as Record<string, unknown> | undefined;
  if (!opts) return false;
  const cols = opts.columns;
  return cols === "full" || (typeof cols === "number" && cols >= 7);
}

// Minimal YAML for the "Show YAML" toggle — plain objects, arrays,
// strings, numbers, booleans, null. Enough for plan operations.
export function toYaml(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (Array.isArray(value)) {
    if (value.length === 0) return `${pad}[]`;
    return value
      .map((item) => {
        if (item !== null && typeof item === "object") {
          const inner = toYaml(item, indent + 1).trimStart();
          return `${pad}- ${inner}`;
        }
        return `${pad}- ${scalar(item)}`;
      })
      .join("\n");
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return `${pad}{}`;
    return entries
      .map(([k, v]) => {
        if (v !== null && typeof v === "object") {
          const isEmpty = Array.isArray(v) ? v.length === 0 : Object.keys(v).length === 0;
          if (isEmpty) return `${pad}${k}: ${Array.isArray(v) ? "[]" : "{}"}`;
          return `${pad}${k}:\n${toYaml(v, indent + 1)}`;
        }
        return `${pad}${k}: ${scalar(v)}`;
      })
      .join("\n");
  }
  return `${pad}${scalar(value)}`;
}

function scalar(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return /^[A-Za-z0-9_./:-]+$/.test(v) && v !== "" ? v : JSON.stringify(v);
  return String(v);
}
```

Note on the nested-array YAML: `toYaml(item, indent + 1).trimStart()` puts the first key on the `- ` line and later keys at `indent + 1`, which is valid YAML for a list of mappings.

- [ ] **Step 2: Type-check**

Run: `cd ui && npx tsc -b --noEmit`
Expected: clean. If `React.ReactNode` is not in scope, add `import type { ReactNode } from "react";` and use `ReactNode`.

- [ ] **Step 3: Commit**

```bash
git add ui/src/components/DashboardPlanCard.tsx
git commit -m "feat(ui): DashboardPlanCard wireframe with Apply / Modify / Show YAML

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 16: Changelog and release

**Files:**
- Modify: `CHANGELOG.md` (insert above the newest entry, which is `1.5.0b6` if sub-project 1 shipped)

- [ ] **Step 1: Write the entry**

```markdown
## [1.5.0b7] — 2026-09-19

> ⚠️ **BETA — test at your own risk.** Continues the 1.5.0 beta. Back up your `context.yaml` before updating.

### Added
- **Dashboards are now planned, shown, approved, then applied.** Ask for a change and Mylo stages a plan: every view, section, and card it intends to create or move, drawn as a wireframe in the chat with the choices it made without asking listed underneath. Apply runs exactly that plan; Modify sends it back for changes; Show YAML reveals the operations. The Apply click is bound to the plan you saw, so Mylo cannot apply something different.
- **Cards go where you asked.** New `position` (start / end / index) on every insert, a `move_card` operation, and sections addressed by their heading text ("put the thermostat at the top of Climate").
- **Every apply is verified against the plan.** After the single save, Mylo reads the dashboard back and confirms each operation landed at its intended section and index, reporting per-operation results. A JSON backup of the previous dashboard is written under `.mylo/dashboard_backups/` before every apply (last 20 kept).
- **Card options are checked before you approve.** A `tile` without an entity, a `conditional` without conditions, bad `grid_options`, and similar are caught at plan time. Energy cards, clock, and shopping-list no longer trigger spurious "unrecognized card" warnings.
- `query_dashboard` now lists each section's heading and each card's index, type, and entity.

### Changed
- **Per-card width is now set the way Home Assistant expects.** Mylo uses `grid_options.columns` on the card for graphs, maps, and weather; a section's `column_span` is reserved for whole-section widening.
- `modify_dashboard` has been removed; `plan_dashboard` and `apply_dashboard_plan` replace it. Dashboard changes no longer use the dry-run flag.

### Fixed
- **Answering a question no longer silently approves a pending write.** Tapping an option on a question card used to send the approval flag if a preview was also waiting. Only the Apply button approves now, for every tool.
- `verify_change dashboard_loaded` no longer flags heading-only sections as malformed and no longer waits five seconds for a save that applies instantly.
```

- [ ] **Step 2: Commit the changelog**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): 1.5.0b7 — dashboard plan flow

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 3: Cut the release (only when the user says to release)**

Run: `DRY_RUN=1 scripts/release.sh 1.5.0b7` and read its output. Then, with the user's go-ahead, `scripts/release.sh 1.5.0b7`.

---

## Manual verification in Home Assistant (after rebuild)

Not automatable here; the user runs these against the real instance:

1. "Create a Kitchen view with lights and climate, thermostat first in Climate." Expect: one `ask_user` if no theme preference is stored, then a plan card with two section boxes and the thermostat chip first after the heading, an assumptions line, Apply/Modify/Show YAML. Apply → result lists `all_ok: true` and a backup path. Open the view in HA: thermostat sits at the top of Climate.
2. "Move the hall light tile from Lights to Climate, at the end." Expect: a one-op plan naming `tile · light.hall`, no rewrite of the view.
3. Tap an option on a question card while a plan is pending. Expect: no write happens; the plan card is still there afterwards.
4. Edit the dashboard in HA's UI between plan and Apply so the targeted card moves. Expect: `target_changed` with the actual card named, nothing written.
5. Show YAML toggles the operations; Modify focuses the composer with "Change the plan: ".
