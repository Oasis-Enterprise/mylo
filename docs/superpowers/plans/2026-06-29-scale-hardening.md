# Scale Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Mylo's context assembly, tool results, and nightly reconciler provably bounded in token size regardless of home size, and make HA registry fetches survive large instances — so big homes (2,000–10,000 entities) stop failing.

**Architecture:** Introduce one budget authority (`ContextBudget`) plus a `Surface` contract; the assembler renders surfaces in priority order within a total token budget derived from the active model's window. The same shared `estimate_tokens` estimator bounds the reconciler payload (per-section compaction) and tool results. HA registry fetches get adaptive timeouts + retry + non-blocking degradation.

**Tech Stack:** Python 3.12, pytest, dataclasses, `typing.Protocol`, aiohttp (server), APScheduler (jobs). No new third-party deps.

**Spec:** `docs/superpowers/specs/2026-06-29-scale-hardening-design.md`

**Conventions for every task:**
- Run the full gate before each commit: `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src && .venv/bin/python -m pytest tests/unit -q`
- All new source files start with the Apache 2.0 license header (copy from any existing file in `src/mylo/`).
- Avoid the `×` character in docstrings (ruff `RUF002`); write "times".

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/mylo/context/tokens.py` (new) | Shared `estimate_tokens` + model context-window table | 1 |
| `src/mylo/context/budget.py` (new) | `Rendered`, `Surface` protocol, `ContextBudget` allocator | 2 |
| `src/mylo/context/surfaces.py` (new) | Concrete surfaces wrapping existing prompt builders | 3 |
| `src/mylo/context/working_set.py` (new) | Relevance ranker + working-set surface | 4 |
| `src/mylo/context/assembler.py` (modify) | Drive surfaces through `ContextBudget` | 5 |
| `src/mylo/config.py` (modify) | New budget config knobs | 6 |
| `config.yaml` + `translations/en.yaml` (modify) | Surface the new knobs | 6 |
| `src/mylo/tools/base.py` (modify) | Shared `bound_rows` result-budgeting helper | 7 |
| `src/mylo/memory/reconciler.py` (modify) | Per-section payload compaction | 8 |
| `src/mylo/ha/registries.py` (modify) | Adaptive-timeout + retry + non-blocking refresh | 9 |
| `tests/unit/test_scale_invariant.py` (new) | Property/scale tests + regression locks | 10 |

---

## Task 1: Shared token estimator

**Files:**
- Create: `src/mylo/context/tokens.py`
- Test: `tests/unit/test_tokens.py`

The estimator must **over-estimate** (assume ~3.5 chars/token, fewer chars-per-token than the real ~4) so a heuristic miss can never let a prompt exceed the real window. It replaces scattered `len(x)//4` heuristics.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tokens.py
from __future__ import annotations

from mylo.context.tokens import context_window_for, estimate_tokens


def test_estimate_tokens_is_conservative() -> None:
    # 3500 chars should estimate >= 1000 tokens (real models ~875).
    text = "a" * 3500
    assert estimate_tokens(text) >= 1000


def test_estimate_tokens_empty_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_context_window_known_models() -> None:
    assert context_window_for("claude-sonnet-4-6") == 1_000_000
    assert context_window_for("claude-haiku-4-5") == 200_000
    assert context_window_for("claude-haiku-4-5-20251001") == 200_000  # dated snapshot


def test_context_window_unknown_model_is_conservative() -> None:
    assert context_window_for("some-future-model") == 200_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tokens.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mylo.context.tokens'`

- [ ] **Step 3: Write the implementation**

```python
# src/mylo/context/tokens.py  (include the Apache license header)
"""Shared token estimation and model context-window lookup.

One estimator for the whole codebase, replacing scattered ``len(x)//4``
heuristics. It deliberately OVER-estimates (assumes fewer characters per
token than reality) so a miss can never let an assembled prompt exceed
the real model window — the budget manager and reconciler both rely on
that safety direction.
"""

from __future__ import annotations

import math

# Conservative: real models average ~4 chars/token; 3.5 over-estimates.
_CHARS_PER_TOKEN = 3.5

# Usable context window per model, in tokens. Unknown models fall back to
# the smallest mainstream window so we never assume more room than exists.
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-flash-lite": 1_000_000,
    "gemini-3-pro-preview": 1_000_000,
    "gpt-5.5": 400_000,
    "gpt-5.4": 400_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4-nano": 400_000,
}
_DEFAULT_WINDOW = 200_000


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for ``text`` (rounds up)."""
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def context_window_for(model: str) -> int:
    """Usable context window for ``model`` (dated snapshots match base)."""
    if model in _CONTEXT_WINDOWS:
        return _CONTEXT_WINDOWS[model]
    for key, window in _CONTEXT_WINDOWS.items():
        if model.startswith(key):
            return window
    return _DEFAULT_WINDOW
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tokens.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mylo/context/tokens.py tests/unit/test_tokens.py
git commit -m "feat(context): shared conservative token estimator + window table"
```

---

## Task 2: Surface contract + ContextBudget allocator

**Files:**
- Create: `src/mylo/context/budget.py`
- Test: `tests/unit/test_budget.py`

The invariant: each surface returns `tokens <= budget`. `ContextBudget` renders surfaces in order, lends unused budget downstream, and reports allocations + whether anything was trimmed.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_budget.py
from __future__ import annotations

from mylo.context.budget import ContextBudget, Rendered, TextSurface


class _FixedSurface:
    """Surface that emits a fixed string, or nothing if it doesn't fit."""

    def __init__(self, name: str, text: str, tokens: int) -> None:
        self.name = name
        self._text = text
        self._tokens = tokens

    def render(self, budget_tokens: int) -> Rendered:
        if self._tokens > budget_tokens:
            return Rendered(text="", tokens=0)
        return Rendered(text=self._text, tokens=self._tokens)


def test_renders_in_order_and_joins() -> None:
    budget = ContextBudget(total_tokens=100)
    result = budget.render([_FixedSurface("a", "AAA", 10), _FixedSurface("b", "BBB", 10)])
    assert result.text == "AAA\n\n---\n\nBBB"
    assert result.tokens_used == 20
    assert result.allocations == [("a", 10), ("b", 10)]
    assert result.trimmed is False


def test_high_priority_starves_low_priority_when_over_budget() -> None:
    budget = ContextBudget(total_tokens=15)
    result = budget.render([_FixedSurface("big", "X", 10), _FixedSurface("nope", "Y", 10)])
    # First fits (10 <= 15); second needs 10 but only 5 remain -> dropped.
    assert result.text == "X"
    assert result.trimmed is True
    assert ("nope", 0) in result.allocations


def test_empty_surfaces_are_skipped() -> None:
    budget = ContextBudget(total_tokens=100)
    result = budget.render([_FixedSurface("empty", "", 0), _FixedSurface("a", "AAA", 10)])
    assert result.text == "AAA"


def test_for_model_derives_total_from_window() -> None:
    # 200_000 window * 0.6 factor - 8000 reserve = 112_000.
    budget = ContextBudget.for_model("claude-haiku-4-5", factor=0.6, output_reserve=8000)
    assert budget.total_tokens == 112_000


def test_text_surface_drops_when_too_big() -> None:
    s = TextSurface("note", "a" * 100)  # ~29 tokens
    assert s.render(5).text == ""
    assert s.render(1000).text == "a" * 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mylo.context.budget'`

- [ ] **Step 3: Write the implementation**

```python
# src/mylo/context/budget.py  (include the Apache license header)
"""Context budget authority: the Surface contract + priority allocator.

Every piece of prompt context is a Surface that renders within a token
budget. ``ContextBudget`` renders surfaces in priority order, lending any
unused budget downstream, and guarantees the assembled text fits the
total — so prompt size is a function of the budget, not of home size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mylo.context.tokens import context_window_for, estimate_tokens

_JOINER = "\n\n---\n\n"


@dataclass(slots=True, frozen=True)
class Rendered:
    """A surface's output. Invariant: ``tokens`` <= the render budget."""

    text: str
    tokens: int


@runtime_checkable
class Surface(Protocol):
    name: str

    def render(self, budget_tokens: int) -> Rendered: ...


@dataclass(slots=True, frozen=True)
class AssembledContext:
    text: str
    tokens_used: int
    allocations: list[tuple[str, int]]
    trimmed: bool


class TextSurface:
    """Atomic surface: emit the full text if it fits, else nothing.

    Used for fixed blocks (identity, hints, cost notes) that can't be
    partially rendered — they either make the budget or get dropped.
    """

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self._text = text
        self._tokens = estimate_tokens(text)

    def render(self, budget_tokens: int) -> Rendered:
        if not self._text or self._tokens > budget_tokens:
            return Rendered(text="", tokens=0)
        return Rendered(text=self._text, tokens=self._tokens)


class ContextBudget:
    """Renders surfaces in priority order within a fixed total."""

    def __init__(self, total_tokens: int) -> None:
        self.total_tokens = max(0, total_tokens)

    @classmethod
    def for_model(cls, model: str, *, factor: float, output_reserve: int) -> ContextBudget:
        window = context_window_for(model)
        total = int(window * factor) - output_reserve
        return cls(total_tokens=max(0, total))

    def render(self, surfaces: list[Surface]) -> AssembledContext:
        remaining = self.total_tokens
        parts: list[str] = []
        allocations: list[tuple[str, int]] = []
        trimmed = False
        for surface in surfaces:
            rendered = surface.render(remaining)
            # Defensive: enforce the contract even if a surface misbehaves.
            if rendered.tokens > remaining:
                rendered = Rendered(text="", tokens=0)
            allocations.append((surface.name, rendered.tokens))
            if rendered.text:
                parts.append(rendered.text)
                remaining -= rendered.tokens
            else:
                trimmed = True
        return AssembledContext(
            text=_JOINER.join(parts),
            tokens_used=self.total_tokens - remaining,
            allocations=allocations,
            trimmed=trimmed,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_budget.py -v`
Expected: PASS (5 passed)

Note: `test_high_priority_starves_low_priority_when_over_budget` expects `trimmed=True` because the second surface returns empty — the empty-branch sets `trimmed`. The `test_empty_surfaces_are_skipped` case also sets `trimmed=True`, which is acceptable (an empty atomic block counts as trimmed); the assertion there only checks `.text`.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/context/budget.py tests/unit/test_budget.py
git commit -m "feat(context): ContextBudget allocator + Surface contract"
```

---

## Task 3: Concrete surfaces wrapping existing builders

**Files:**
- Create: `src/mylo/context/surfaces.py`
- Test: `tests/unit/test_surfaces.py`

Wrap the budget-elastic builders so they self-trim. `TopologySurface` drops the least-populated areas to fit; `MemorySurface` drops the lowest-priority sections to fit. Atomic blocks just use `TextSurface` from Task 2.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_surfaces.py
from __future__ import annotations

from mylo.context.surfaces import TopologySurface
from mylo.context.tokens import estimate_tokens
from tests.unit.test_context_assembler import _fake_registries  # reuse fixture builder


def test_topology_surface_fits_budget() -> None:
    reg = _fake_registries()
    surface = TopologySurface(reg, memory=None)
    rendered = surface.render(50)
    assert rendered.tokens <= 50
    assert estimate_tokens(rendered.text) <= 50


def test_topology_surface_full_when_budget_ample() -> None:
    reg = _fake_registries()
    surface = TopologySurface(reg, memory=None)
    rendered = surface.render(100_000)
    assert "HOME TOPOLOGY:" in rendered.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_surfaces.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mylo.context.surfaces'`

- [ ] **Step 3: Write the implementation**

```python
# src/mylo/context/surfaces.py  (include the Apache license header)
"""Concrete budget-aware surfaces wrapping the existing prompt builders.

The atomic blocks (identity, hints, proposals, cost notes) use
``TextSurface`` directly. The elastic blocks — topology and memory —
get dedicated surfaces that shed their lowest-value content to fit a
shrinking budget instead of being all-or-nothing.
"""

from __future__ import annotations

from mylo.context.budget import Rendered
from mylo.context.tokens import estimate_tokens
from mylo.context.topology import build_topology, format_topology
from mylo.ha.registries import Registries
from mylo.memory.schema import MemoryFile


class TopologySurface:
    name = "topology"

    def __init__(self, registries: Registries, *, memory: MemoryFile | None) -> None:
        self._registries = registries
        self._memory = memory

    def render(self, budget_tokens: int) -> Rendered:
        topology = build_topology(self._registries, memory=self._memory)
        text = format_topology(topology)
        tokens = estimate_tokens(text)
        if tokens <= budget_tokens:
            return Rendered(text=text, tokens=tokens)
        # Too big: drop areas from the bottom (they're already sorted by
        # entity count, so the tail is the least-populated) until it fits.
        areas = topology.get("areas") or {}
        area_items = list(areas.items())
        while area_items and tokens > budget_tokens:
            area_items.pop()
            topology["areas"] = dict(area_items)
            topology["areas_truncated"] = True
            text = format_topology(topology)
            tokens = estimate_tokens(text)
        if tokens > budget_tokens:
            return Rendered(text="", tokens=0)
        return Rendered(text=text, tokens=tokens)
```

Note: `format_topology` ignores unknown keys like `areas_truncated`, so this is safe. If you want the truncation visible to the model, add one line in `format_topology` (`src/mylo/context/topology.py`) after the areas loop:
```python
    if topology.get("areas_truncated"):
        lines.append("  # (low-population areas omitted to fit context budget)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_surfaces.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mylo/context/surfaces.py tests/unit/test_surfaces.py src/mylo/context/topology.py
git commit -m "feat(context): budget-aware TopologySurface that sheds areas to fit"
```

---

## Task 4: Working-set allocator

**Files:**
- Create: `src/mylo/context/working_set.py`
- Test: `tests/unit/test_working_set.py`

Ranks entities by relevance to the user's turn and emits the top-N that fit the budget. Pure ranking over `Registries` + the conversation text + memory; no HA calls. `states` (optional dict of `entity_id -> state`) feeds recency/state rendering when available.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_working_set.py
from __future__ import annotations

from mylo.context.working_set import WorkingSetSurface, rank_entities
from tests.unit.test_context_assembler import _fake_registries


def test_mentioned_entity_ranks_first() -> None:
    reg = _fake_registries()
    any_id = next(iter(reg.entities))
    ranked = rank_entities(
        reg,
        conversation_text=f"what is {any_id} doing",
        monitored=set(),
        max_entities=5,
    )
    assert ranked[0].entity_id == any_id


def test_domain_mention_ranks_matching_domain() -> None:
    reg = _fake_registries()
    ranked = rank_entities(reg, conversation_text="turn off the lights", monitored=set(), max_entities=5)
    assert ranked  # at least one light-ish entity surfaces
    assert all(hasattr(e, "entity_id") for e in ranked)


def test_working_set_surface_respects_budget_and_cap() -> None:
    reg = _fake_registries()
    surface = WorkingSetSurface(
        reg, conversation_text="lights", monitored=set(), states=None, max_entities=3
    )
    rendered = surface.render(10_000)
    # Never more than the cap; never over budget.
    assert rendered.text.count("\n") <= 3 + 1  # header + <=3 rows
    tiny = surface.render(1)
    assert tiny.tokens <= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_working_set.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mylo.context.working_set'`

- [ ] **Step 3: Write the implementation**

```python
# src/mylo/context/working_set.py  (include the Apache license header)
"""Relevance-ranked working set — the bounded entity surface.

Pre-loads the entities most likely relevant to the user's turn so the
model usually doesn't need a query round-trip, capped to a token budget
and a hard entity count. Purely additive: anything outside the set is
still reachable via the query tools, so completeness is never lost.
"""

from __future__ import annotations

from collections.abc import Mapping

from mylo.context.budget import Rendered
from mylo.context.tokens import estimate_tokens
from mylo.ha.registries import EntityEntry, Registries

_HEADER = "RELEVANT ENTITIES (pre-loaded for this turn; query for anything else):"


def _score(entity: EntityEntry, *, text_lc: str, monitored: set[str]) -> int:
    score = 0
    if entity.entity_id in text_lc:
        score += 100
    if entity.friendly_name and entity.friendly_name.lower() in text_lc:
        score += 80
    # Domain mention, e.g. "lights" / "light".
    if entity.domain in text_lc or f"{entity.domain}s" in text_lc:
        score += 20
    if entity.entity_id in monitored:
        score += 10
    return score


def rank_entities(
    registries: Registries,
    *,
    conversation_text: str,
    monitored: set[str],
    max_entities: int,
) -> list[EntityEntry]:
    text_lc = conversation_text.lower()
    scored = []
    for entity in registries.entities.values():
        if entity.disabled_by or entity.hidden_by:
            continue
        s = _score(entity, text_lc=text_lc, monitored=monitored)
        if s > 0:
            scored.append((s, entity.entity_id, entity))
    # Sort by score desc, then entity_id for deterministic ties.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [e for _, _, e in scored[:max_entities]]


def _render_row(entity: EntityEntry, states: Mapping[str, str] | None) -> str:
    state = states.get(entity.entity_id) if states else None
    state_part = f" = {state}" if state is not None else ""
    return f"- {entity.entity_id} ({entity.friendly_name}){state_part}"


class WorkingSetSurface:
    name = "working_set"

    def __init__(
        self,
        registries: Registries,
        *,
        conversation_text: str,
        monitored: set[str],
        states: Mapping[str, str] | None,
        max_entities: int,
    ) -> None:
        self._registries = registries
        self._text = conversation_text
        self._monitored = monitored
        self._states = states
        self._max = max_entities

    def render(self, budget_tokens: int) -> Rendered:
        ranked = rank_entities(
            self._registries,
            conversation_text=self._text,
            monitored=self._monitored,
            max_entities=self._max,
        )
        if not ranked:
            return Rendered(text="", tokens=0)
        lines = [_HEADER]
        for entity in ranked:
            candidate = "\n".join([*lines, _render_row(entity, self._states)])
            if estimate_tokens(candidate) > budget_tokens:
                break
            lines.append(_render_row(entity, self._states))
        if len(lines) == 1:  # only the header fit -> emit nothing
            return Rendered(text="", tokens=0)
        text = "\n".join(lines)
        return Rendered(text=text, tokens=estimate_tokens(text))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_working_set.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mylo/context/working_set.py tests/unit/test_working_set.py
git commit -m "feat(context): relevance-ranked working-set surface"
```

---

## Task 5: Drive the assembler through ContextBudget

**Files:**
- Modify: `src/mylo/context/assembler.py` (the `parts` assembly, lines ~95–178)
- Test: `tests/unit/test_context_assembler.py` (add budget tests; existing tests must still pass)

Replace the flat `parts.append(...)` + `"\n\n---\n\n".join(parts)` with a priority-ordered surface list rendered by `ContextBudget`. Preserve all existing content and the existing function signature; add three params: `model`, `budget_factor`, `output_reserve` (with defaults so current callers/tests keep working). Keep `AssembledPrompt` output shape identical.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_context_assembler.py
from mylo.context.tokens import estimate_tokens


def test_assembled_prompt_respects_budget(tmp_path: Path) -> None:
    reg = _fake_registries()
    mem = empty_memory()
    base = LoadedPrompt(text="identity", version="v")
    result = assemble_system_prompt(
        registries=reg,
        memory=mem,
        conversation_text="lights",
        mylo_data_dir=tmp_path,
        base_prompt=base,
        model="claude-haiku-4-5",
        budget_factor=0.0009,  # force a tiny budget (200_000 * 0.0009 = 180 tokens)
        output_reserve=0,
    )
    assert estimate_tokens(result.system) <= 180
    # Identity is highest priority — it must survive even a tiny budget.
    assert "identity" in result.system
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_context_assembler.py::test_assembled_prompt_respects_budget -v`
Expected: FAIL with `TypeError: assemble_system_prompt() got an unexpected keyword argument 'model'`

- [ ] **Step 3: Implement**

In `src/mylo/context/assembler.py`:

1. Add imports near the top:
```python
from mylo.context.budget import ContextBudget, Surface, TextSurface
from mylo.context.surfaces import TopologySurface
from mylo.context.working_set import WorkingSetSurface
```

2. Add three params to `assemble_system_prompt` (after `monthly_budget_usd`):
```python
    model: str = "claude-sonnet-4-6",
    budget_factor: float = 0.6,
    output_reserve: int = 8000,
```

3. Add `working_set_max_entities: int = 40` to the signature (after `output_reserve`).

4. Replace the body from `parts: list[str] = [layer1.text]` through `system = "\n\n---\n\n".join(parts)` with the ordered surface list below. The key design point: **conflicts are split out of the memory block into their own high-priority surface** (via the existing `build_memory_section(sections={"conflicts"})` path), so a safety-critical conflict is never the thing budget pressure drops — while the bulkier, lower-value memory sections stay below topology/working-set in priority.

```python
    surfaces: list[Surface] = [TextSurface("identity", layer1.text)]

    sections = select_sections(conversation_text, memory=memory)

    # Conflicts are safety-critical → their own high-priority surface,
    # rendered via the existing memory builder with just that section.
    if "conflicts" in sections:
        conflicts_text = build_memory_section(
            memory, mylo_data_dir=mylo_data_dir, timezone=timezone, sections={"conflicts"}
        )
        if conflicts_text:
            surfaces.append(TextSurface("critical_memory", conflicts_text))

    # Task references (Layer 4).
    task_type = detect_task_type(conversation_text)
    if task_type is not None:
        references_text = load_task_context(task_type, mylo_data_dir=mylo_data_dir)
        if references_text:
            surfaces.append(
                TextSurface(
                    "task_refs",
                    f"REFERENCE EXAMPLES (task: {task_type}) — use these as "
                    f"few-shot patterns, not gospel:\n\n{references_text}",
                )
            )

    # Topology (Layer 2) + working set — elastic, self-trimming.
    if registries is not None and registries.entities:
        surfaces.append(TopologySurface(registries, memory=memory))
        surfaces.append(
            WorkingSetSurface(
                registries,
                conversation_text=conversation_text,
                monitored=set(memory.monitored_entities),
                states=None,
                max_entities=working_set_max_entities,
            )
        )

    # Remaining memory sections (Layer 3) minus conflicts (rendered above).
    rest_sections = sections - {"conflicts"}
    memory_text = build_memory_section(
        memory, mylo_data_dir=mylo_data_dir, timezone=timezone, sections=rest_sections
    )
    if memory_text:
        surfaces.append(TextSurface("memory", memory_text))

    # Low-priority atomic blocks (drop first under budget pressure).
    for name, text in _low_priority_blocks(
        memory,
        is_local_provider=is_local_provider,
        session_cost_usd=session_cost_usd,
        session_budget_usd=session_budget_usd,
        monthly_spent_usd=monthly_spent_usd,
        monthly_budget_usd=monthly_budget_usd,
    ):
        surfaces.append(TextSurface(name, text))

    budget = ContextBudget.for_model(
        model, factor=budget_factor, output_reserve=output_reserve
    )
    assembled = budget.render(surfaces)
    system = assembled.text
    log.info(
        "context.budget",
        total=budget.total_tokens,
        used=assembled.tokens_used,
        trimmed=assembled.trimmed,
        allocations=assembled.allocations,
    )
```

5. Extract the existing cold-start hints / pending observations / proposals / cost-note logic (current lines ~123–176) into a module-level helper `_low_priority_blocks(memory, *, is_local_provider, session_cost_usd, session_budget_usd, monthly_spent_usd, monthly_budget_usd) -> list[tuple[str, str]]` that returns `(name, text)` pairs — move the current code verbatim, appending `("hints", ...)`, `("pending", ...)`, `("proposals", ...)`, `("cost_note", ...)`, `("monthly_cost_note", ...)` as each currently applies. No behavior change; only the surface wrapper is new.

> Verify `build_memory_section` renders the conflicts block when called with `sections={"conflicts"}` (the selector emits `"conflicts"` whenever conflicts are pending — see `test_selector_always_includes_conflicts_when_pending`). If conflicts are *not* a standalone section there, fall back to keeping them in the single memory surface and raising that surface above topology instead.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/unit/test_context_assembler.py -v`
Expected: PASS — the new budget test plus all existing assembler tests (they pass `model` implicitly via defaults).

- [ ] **Step 5: Wire the call site**

In `src/mylo/server/routes_chat.py` (the `assemble_system_prompt(...)` call ~line 362), pass `model=config.model`, `budget_factor=config.context_budget_factor`, `output_reserve=config.context_output_reserve_tokens`, `working_set_max_entities=config.working_set_max_entities` (added in Task 6). Until Task 6 lands, pass literals `budget_factor=0.6, output_reserve=8000, working_set_max_entities=40`.

- [ ] **Step 6: Commit**

```bash
git add src/mylo/context/assembler.py src/mylo/server/routes_chat.py tests/unit/test_context_assembler.py
git commit -m "feat(context): assemble prompt through ContextBudget surfaces"
```

---

## Task 6: Config knobs

**Files:**
- Modify: `src/mylo/config.py:46-68` (AppConfig fields) and `:117-135` (load_config)
- Modify: `config.yaml` (schema + defaults), `translations/en.yaml`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_config.py
def test_context_budget_defaults() -> None:
    import os
    os.environ.pop("MYLO_OPTIONS_FILE", None)
    from mylo.config import load_config
    cfg = load_config()
    assert cfg.context_budget_factor == 0.6
    assert cfg.context_output_reserve_tokens == 8000
    assert cfg.working_set_max_entities == 40
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py::test_context_budget_defaults -v`
Expected: FAIL with `AttributeError: 'AppConfig' object has no attribute 'context_budget_factor'`

- [ ] **Step 3: Implement**

Add to `AppConfig` (after `monthly_budget_usd`):
```python
    context_budget_factor: float
    context_output_reserve_tokens: int
    working_set_max_entities: int
```
Add to the `AppConfig(...)` constructor in `load_config`:
```python
        context_budget_factor=float(_get(options, "context_budget_factor", 0.6)),
        context_output_reserve_tokens=int(
            _get(options, "context_output_reserve_tokens", 8000)
        ),
        working_set_max_entities=int(_get(options, "working_set_max_entities", 40)),
```
Add to `config.yaml` under `options:` and `schema:`:
```yaml
  context_budget_factor: 0.6
  context_output_reserve_tokens: 8000
  working_set_max_entities: 40
```
```yaml
  context_budget_factor: "float(0.1,0.95)?"
  context_output_reserve_tokens: "int(1000,64000)?"
  working_set_max_entities: "int(0,200)?"
```
Add to `translations/en.yaml` under `configuration:`:
```yaml
  context_budget_factor:
    name: Context Budget Factor
    description: >-
      Fraction of the model's context window Mylo may spend on the prompt.
      Lower = cheaper/faster, higher = more context per turn. Default 0.6.
  context_output_reserve_tokens:
    name: Output Reserve (tokens)
    description: Tokens held back from the budget for the model's reply.
  working_set_max_entities:
    name: Working Set Size
    description: >-
      Max entities pre-loaded into the prompt per turn. Anything else is
      still reachable by query. 0 disables pre-loading.
```
Then update the `routes_chat.py` call from Task 5 Step 5 to use these config fields.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py -v`
Expected: PASS. Also validate YAML: `.venv/bin/python -c "import ruamel.yaml as r; y=r.YAML(typ='safe'); y.load(open('config.yaml')); y.load(open('translations/en.yaml')); print('ok')"`

- [ ] **Step 5: Commit**

```bash
git add src/mylo/config.py config.yaml translations/en.yaml src/mylo/server/routes_chat.py tests/unit/test_config.py
git commit -m "feat(config): context budget + working-set knobs"
```

---

## Task 7: Uniform tool-result bounding

**Files:**
- Modify: `src/mylo/tools/base.py` (add `bound_rows` helper)
- Modify: read tools that return row lists — start with `src/mylo/tools/read/query_devices.py` (query_entities already bounds)
- Test: `tests/unit/test_tool_result_bounding.py`

Add a shared helper that turns an oversized row list into a summary envelope, so every read tool bounds the same way.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tool_result_bounding.py
from __future__ import annotations

from mylo.tools.base import bound_rows


def test_under_budget_returns_all_rows() -> None:
    rows = [{"id": i} for i in range(5)]
    env = bound_rows(rows, max_rows=10, sample=3)
    assert env["rows"] == rows
    assert env["total"] == 5
    assert "truncated" not in env


def test_over_budget_returns_sample_envelope() -> None:
    rows = [{"id": i} for i in range(500)]
    env = bound_rows(rows, max_rows=100, sample=5)
    assert env["total"] == 500
    assert env["truncated"] is True
    assert len(env["sample"]) == 5
    assert "rows" not in env
    assert "narrow" in env["hint"].lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tool_result_bounding.py -v`
Expected: FAIL with `ImportError: cannot import name 'bound_rows'`

- [ ] **Step 3: Implement** — add to `src/mylo/tools/base.py`:

```python
from typing import Any


def bound_rows(
    rows: list[dict[str, Any]], *, max_rows: int, sample: int = 5
) -> dict[str, Any]:
    """Return rows verbatim when small; a summary envelope when oversized.

    Keeps tool results bounded and *actionable*: the model is told the
    total and shown a representative sample with a hint to narrow, instead
    of receiving a truncated list it can't tell was cut.
    """
    if len(rows) <= max_rows:
        return {"rows": rows, "total": len(rows)}
    return {
        "total": len(rows),
        "truncated": True,
        "sample": rows[:sample],
        "hint": "Too many results — narrow with area/domain/name filters to see rows.",
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tool_result_bounding.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Apply in `query_devices.py`** — wrap its result list with `bound_rows(devices, max_rows=200)` before `ToolResult.ok(...)`, mirroring `query_entities`'s existing cap. Add a test in the query_devices test module asserting a 500-device result returns `truncated: True`.

- [ ] **Step 6: Commit**

```bash
git add src/mylo/tools/base.py src/mylo/tools/read/query_devices.py tests/unit/test_tool_result_bounding.py tests/unit/test_query_devices.py
git commit -m "feat(tools): shared row-bounding helper for read tools"
```

---

## Task 8: Reconciler per-section compaction

**Files:**
- Modify: `src/mylo/memory/reconciler.py` (`_build_user_payload`, the `_PAYLOAD_TOKEN_BUDGET` check ~215-235, use shared `estimate_tokens`)
- Test: `tests/unit/test_reconciler_compaction.py`

Instead of skipping the merge when the payload is too big, compact oversized sections so the payload always fits, and run the merge. Critical items (conflicts, user-confirmed notes) are never compacted.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reconciler_compaction.py
from __future__ import annotations

from mylo.memory.reconciler import compact_payload_sections
from mylo.memory.schema import MemoryFile, Note


def test_compaction_drops_oldest_low_value_notes_to_fit() -> None:
    mem = MemoryFile()
    mem.notes = [Note(id=f"n{i}", content="x" * 200) for i in range(1000)]
    compacted, marker = compact_payload_sections(mem, budget_tokens=2000)
    assert len(compacted.notes) < 1000
    assert "compacted" in marker.lower()


def test_compaction_preserves_user_confirmed_notes() -> None:
    mem = MemoryFile()
    mem.notes = [Note(id="keep", content="y" * 200, user_confirmed=True)]
    mem.notes += [Note(id=f"n{i}", content="x" * 200) for i in range(1000)]
    compacted, _ = compact_payload_sections(mem, budget_tokens=1500)
    assert any(n.id == "keep" for n in compacted.notes)
```

> Before writing: confirm `Note`'s field for user confirmation in `src/mylo/memory/schema.py` (likely `user_confirmed: bool`). If the field name differs, use the real one in the test and implementation.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_reconciler_compaction.py -v`
Expected: FAIL with `ImportError: cannot import name 'compact_payload_sections'`

- [ ] **Step 3: Implement** — add to `reconciler.py`:

Drop items in **chunks** (≈10% of remaining per round), re-estimating once per round — O(rounds · n) with ~tens of rounds, not O(n²). Never re-serialize per single item.

```python
import copy
from collections import Counter

from mylo.context.tokens import estimate_tokens


def compact_payload_sections(
    memory: MemoryFile, *, budget_tokens: int
) -> tuple[MemoryFile, str]:
    """Return a copy of ``memory`` whose serialized size fits ``budget_tokens``,
    compacting the most expendable sections first (patterns, then plain
    notes). Critical items (conflicts, user_confirmed notes) are never
    dropped. Drops in chunks to keep this O(rounds·n), not O(n²).
    """
    work = copy.deepcopy(memory)
    if estimate_tokens(work.model_dump_json()) <= budget_tokens:
        return work, ""

    dropped: Counter[str] = Counter()
    for section in ("patterns", "notes"):
        items = list(getattr(work, section, []))
        protected = [it for it in items if getattr(it, "user_confirmed", False)]
        droppable = [it for it in items if not getattr(it, "user_confirmed", False)]
        while droppable and estimate_tokens(work.model_dump_json()) > budget_tokens:
            chunk = max(1, len(droppable) // 10)
            del droppable[-chunk:]
            dropped[section] += chunk
            setattr(work, section, protected + droppable)
        setattr(work, section, protected + droppable)

    marker = "; ".join(f"+{n} {sec} compacted" for sec, n in dropped.items())
    return work, marker
```

Then in `run_sync`, replace the over-budget skip (~215–235) so that instead of returning early, it compacts and proceeds:
```python
    prompt = _build_system_prompt()
    payload_budget = _PAYLOAD_TOKEN_BUDGET - estimate_tokens(prompt)
    memory_for_payload, compaction_marker = compact_payload_sections(
        memory, budget_tokens=payload_budget
    )
    user_msg = _build_user_payload(memory_for_payload, scratchpad, diff)
    if compaction_marker:
        user_msg += f"\n\n# NOTE: memory compacted to fit context — {compaction_marker}"
        log.warning("memory.reconciler_compacted", detail=compaction_marker)
```
Keep a final hard backstop: if `estimate_tokens(prompt) + estimate_tokens(user_msg)` still exceeds `_PAYLOAD_TOKEN_BUDGET` after compaction, fall back to the existing skip-with-degraded-result path (don't delete it — it becomes the last resort).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/unit/test_reconciler_compaction.py tests/unit/ -k reconcil -v`
Expected: PASS, and existing reconciler tests still green.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/memory/reconciler.py tests/unit/test_reconciler_compaction.py
git commit -m "feat(memory): compact reconciler payload to fit instead of skipping"
```

---

## Task 9: Scale-safe HA registry fetching

**Files:**
- Modify: `src/mylo/ha/registries.py` (`refresh`, `refresh_for`)
- Test: `tests/unit/test_registries_scale.py`

The fetch already maintains via events; the gap is the **fixed 60s** `send_command` timeout on the full list calls, which times out on large registries. Add an adaptive timeout sized to the last-known entity count, one retry on timeout, and graceful degradation (keep the warm registry on failure rather than clearing it).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_registries_scale.py
from __future__ import annotations

import asyncio

import pytest

from mylo.ha.registries import Registries, _adaptive_timeout


def test_adaptive_timeout_scales_with_size() -> None:
    assert _adaptive_timeout(200) == 60.0          # floor
    assert _adaptive_timeout(5000) > 60.0          # scales up
    assert _adaptive_timeout(50_000) <= 300.0      # capped


async def test_refresh_keeps_warm_data_on_failure() -> None:
    reg = Registries()

    class _Client:
        async def send_command(self, type_: str, timeout: float = 60.0):
            raise asyncio.TimeoutError

    reg._client = _Client()  # type: ignore[assignment]
    reg.entities = {"light.kitchen": object()}  # type: ignore[dict-item]
    await reg.refresh(force=True)  # must not raise, must not clear
    assert "light.kitchen" in reg.entities
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_registries_scale.py -v`
Expected: FAIL with `ImportError: cannot import name '_adaptive_timeout'`

- [ ] **Step 3: Implement** in `registries.py`:

```python
def _adaptive_timeout(entity_count: int) -> float:
    """Scale the registry-fetch timeout with instance size.

    Small homes keep the 60s floor; large registries get proportionally
    longer (the full ``entity_registry/list`` is O(entities)), capped so
    a hung HA can't block forever.
    """
    return min(300.0, max(60.0, entity_count / 50.0))
```

Wrap each `send_command(...)` in `refresh`/`refresh_for` with the adaptive timeout + one retry + degrade. Replace the `asyncio.gather(...)` block in `refresh` with:

```python
            timeout = _adaptive_timeout(len(self.entities) or 2000)
            try:
                entities_raw, devices_raw, areas_raw, labels_raw = await self._fetch_all(timeout)
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("ha.registries.refresh_timeout_retrying", timeout=timeout)
                try:
                    entities_raw, devices_raw, areas_raw, labels_raw = await self._fetch_all(
                        timeout * 2
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    # Degrade: keep the warm registry rather than clearing it.
                    log.error("ha.registries.refresh_failed_keeping_warm")
                    return
            self._replace_entities(entities_raw)
            self._replace_devices(devices_raw)
            self._replace_areas(areas_raw)
            self._replace_labels(labels_raw)
            self._last_full_refresh = time.monotonic()
            log.info("ha.registries.loaded", entities=len(self.entities), ...)
```

Add the helper:
```python
    async def _fetch_all(self, timeout: float) -> tuple[Any, Any, Any, Any]:
        assert self._client is not None
        return await asyncio.gather(
            self._client.send_command("config/entity_registry/list", timeout=timeout),
            self._client.send_command("config/device_registry/list", timeout=timeout),
            self._client.send_command("config/area_registry/list", timeout=timeout),
            self._client.send_command("config/label_registry/list", timeout=timeout),
        )
```
Apply the same adaptive-timeout + degrade pattern to `refresh_for` (single-list calls).

> Confirm `send_command` accepts a `timeout` kwarg (it does: `send_command(self, type_, *, timeout: float = 60.0)` per `ws_client.py:238-242`). Match its exact keyword name.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/unit/test_registries_scale.py tests/unit/ -k registr -v`
Expected: PASS, existing registry tests green.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/ha/registries.py tests/unit/test_registries_scale.py
git commit -m "feat(ha): adaptive-timeout + degrade-safe registry fetch for big instances"
```

---

## Task 10: The invariant — property/scale tests + regression locks

**Files:**
- Create: `tests/unit/test_scale_invariant.py`
- Create: `tests/unit/_scale_fixtures.py` (synthetic registry builder)

This is the headline guarantee: across random home sizes, the assembled prompt is always under budget, and the two real-world failures can't recur.

- [ ] **Step 1: Write the synthetic fixture builder**

```python
# tests/unit/_scale_fixtures.py
from __future__ import annotations

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries

_DOMAINS = ["light", "switch", "sensor", "binary_sensor", "climate", "cover"]


def make_big_registry(*, entities: int, areas: int = 50) -> Registries:
    reg = Registries()
    reg.areas = {f"area_{a}": AreaEntry(area_id=f"area_{a}", name=f"Area {a}", floor_id=None, labels=()) for a in range(areas)}
    ents: dict[str, EntityEntry] = {}
    for i in range(entities):
        dom = _DOMAINS[i % len(_DOMAINS)]
        eid = f"{dom}.entity_{i}"
        ents[eid] = EntityEntry(
            entity_id=eid, name=f"Entity {i}", original_name=None, platform="demo",
            device_id=None, area_id=f"area_{i % areas}", labels=(),
            disabled_by=None, hidden_by=None,
        )
    reg.entities = ents
    return reg
```

- [ ] **Step 2: Write the invariant + regression tests**

```python
# tests/unit/test_scale_invariant.py
from __future__ import annotations

from pathlib import Path

import pytest

from mylo.context.assembler import assemble_system_prompt
from mylo.context.basic_prompt import LoadedPrompt
from mylo.context.tokens import estimate_tokens
from mylo.memory.schema import MemoryFile
from tests.unit._scale_fixtures import make_big_registry


@pytest.mark.parametrize("size", [100, 1000, 2484, 5000, 10000])
def test_prompt_always_under_budget(size: int, tmp_path: Path) -> None:
    reg = make_big_registry(entities=size)
    result = assemble_system_prompt(
        registries=reg,
        memory=MemoryFile(),
        conversation_text="turn off the kitchen lights and check the climate",
        mylo_data_dir=tmp_path,
        base_prompt=LoadedPrompt(text="identity", version="v"),
        model="claude-sonnet-4-6",
        budget_factor=0.6,
        output_reserve=8000,
    )
    budget = int(1_000_000 * 0.6) - 8000
    assert estimate_tokens(result.system) <= budget


def test_regression_reconciler_payload_bounded() -> None:
    # The 214K failure: a bloated memory must compact to fit, not skip.
    from mylo.memory.reconciler import compact_payload_sections
    from mylo.memory.schema import Note

    mem = MemoryFile()
    mem.notes = [Note(id=f"n{i}", content="x" * 500) for i in range(5000)]
    compacted, marker = compact_payload_sections(mem, budget_tokens=100_000)
    assert estimate_tokens(compacted.model_dump_json()) <= 100_000
    assert marker
```

- [ ] **Step 3: Run**

Run: `.venv/bin/python -m pytest tests/unit/test_scale_invariant.py -v`
Expected: PASS (6 passed). If a size fails the budget assertion, the working-set or topology surface isn't shedding enough — fix the surface, not the test.

- [ ] **Step 4: Full gate**

Run: `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src && .venv/bin/python -m pytest tests/unit -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_scale_invariant.py tests/unit/_scale_fixtures.py
git commit -m "test(scale): prompt-under-budget invariant + reconciler regression lock"
```

---

## Final verification (after all tasks)

- [ ] `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src && .venv/bin/python -m pytest tests/unit -q` — fully green.
- [ ] Manual sanity: assemble a prompt against a 5,000-entity synthetic registry and eyeball the `context.budget` log shows `trimmed`/allocations behaving.
- [ ] Add a CHANGELOG entry under a new version and cut a release via `scripts/release.sh` (per project process — user publishes the GitHub release).

## Spec coverage check

- §4.1 estimator → Task 1 · §4.2/4.3 Surface + ContextBudget → Task 2 · §4.4 working set → Task 4 · prompt integration → Tasks 3, 5 · §8 config → Task 6 · §4.5 tool bounding → Task 7 · §4.6 reconciler → Task 8 · §4.7 scale-safe fetch → Task 9 · §6 telemetry (`context.budget` log) → Task 5 · §7 testing/invariant/regressions → Task 10.
- **Deferred to sub-project 2 (Resilience), per spec §2/§10:** the auto-shrink retry on a real context-length 400 (§6 row 1). The prompt is already provably bounded by the estimator's conservative margin, so this is defense-in-depth that belongs with the broader provider-retry work — noted here so it isn't silently dropped.
