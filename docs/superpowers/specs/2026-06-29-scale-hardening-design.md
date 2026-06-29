# Scale Hardening — Design

**Status:** Approved design, ready for implementation planning
**Date:** 2026-06-29
**Part of:** the Robustness pillar (sub-project 1 of 4)

---

## 1. Context

Mylo is gaining real users, and real instances are large — the reference
instance has **2,484 entities**. At that scale, three classes of failure
have shown up in the wild:

- **Memory sync fails** — the reconciler built a 214,368-token prompt and
  hit the model's 200K window (`prompt is too long`), so memory stopped
  syncing.
- **HA registry fetches time out** — `config/entity_registry/list` times
  out after 60s on a large registry, degrading startup and topology.
- **Large tool results** — broad entity/device queries can return
  thousands of rows.

These are being fixed reactively, one pasted log at a time. This spec
replaces the whack-a-mole with a **systematic guarantee**: no code path
emits unbounded data, and the assembled prompt size is a function of a
*budget*, not of *home size*. A 5,000-entity home produces a
same-sized prompt as a 200-entity one — it just spends its working-set
budget more selectively.

This is the first of four robustness sub-projects (Scale → Resilience →
Diagnostics → Failure-UX), each scoped as its own spec. The user-chosen
scale strategy is **"bounded working set, always"**: one uniform code
path, predictable cost at any size, with anything outside the set
reachable on demand via the model's existing query tools.

---

## 2. Goals & non-goals

### Goals

1. The assembled system prompt is **provably ≤ a token budget** for any
   home size (the headline invariant).
2. **No path emits unbounded data** — prompt, tool results, and the
   nightly reconciler each fit their budget or summarize to fit, visibly.
3. **HA registry access stops timing out** on large instances — fetch
   once, maintain incrementally, never block a turn on a full refetch.
4. Every scale limit **degrades visibly and safely** — a clear note, a
   safe fallback, never a stack trace or a 400.
5. Completeness is preserved: bounding the *working set* never hides data
   permanently — the model can always query for anything outside it.

### Non-goals

- General reconnect/retry/circuit-breaker machinery for HA and LLM
  providers — that is **sub-project 2 (Resilience)**. This spec owns only
  the scale-specific HA-fetch parts (adaptive timeout sizing, killing
  repeated full fetches, incremental maintenance).
- Exact token counting via a provider API. We use a fast local estimator
  with a safety margin (§4.1); precision is not required, boundedness is.
- Changing the four-layer prompt model or the memory schema. We make the
  existing surfaces budget-aware; we do not redesign them.

### Success criteria

On a synthetic 5,000-entity / 100-area / 1,000-device home:

- Assembled prompt is under budget **every turn** (property-tested across
  random sizes 100–10,000).
- **No full registry refetch** after the one-time bootstrap.
- The reconciler **completes** (merge runs on a compacted payload).
- **Zero** context-length 400s.
- Assembly stays fast (target: low tens of milliseconds).
- The two named real-world failures (214K reconciler, 60s registry
  timeout) are **regression-locked** and cannot recur.

---

## 3. Architecture overview

One **budget authority** governs all context assembly. Today
`assemble_system_prompt` appends prompt "parts" with no global size
awareness, and the reconciler and HA-fetch paths carry their own ad-hoc
limits. We unify these behind a single `ContextBudget` and a `Surface`
contract, reused by both the prompt assembler and the reconciler so there
is **one budgeting concept** in the codebase, not several.

```
                         ┌─────────────────────────┐
   active model window → │     ContextBudget       │ total = window·factor − output_reserve
                         │  (priority allocator)   │
                         └───────────┬─────────────┘
                                     │ render(remaining) for each, in priority order
        ┌───────────┬───────────┬───┴────┬───────────┬────────────┐
        ▼           ▼           ▼        ▼           ▼            ▼
     Identity   Critical    Task     Topology   Working set   Memory
     (static)   memory      refs     (Layer 2)  (ranked)      sections   … hints/proposals
        each: render(budget) -> Rendered(text, tokens)   with tokens ≤ budget
```

- **Prompt size becomes a function of the budget, not the home.**
- The same `estimate_tokens` + `Surface`/budget primitives bound the
  **reconciler** payload (§4.6).
- **HA registries** are bootstrapped once and maintained via update
  events, so assembly always reads a warm registry (§4.7).

---

## 4. Components

### 4.1 Token estimator (`src/mylo/context/tokens.py`)

A single shared `estimate_tokens(text: str) -> int`, replacing today's
scattered `len(...) // 4` heuristics (e.g. `tool_loop.py`). Heuristic
(chars/4-class) with a built-in **safety margin** so an underestimate
cannot blow the real window. Used by the budget manager, every surface,
the tool-result bounder, and the reconciler. Single source of truth for
"how big is this."

### 4.2 The `Surface` contract (`src/mylo/context/budget.py`)

```python
@dataclass
class Rendered:
    text: str
    tokens: int   # invariant: tokens <= the budget passed to render()

class Surface(Protocol):
    name: str
    def render(self, budget_tokens: int) -> Rendered: ...
```

A surface that cannot say anything useful within its budget returns an
empty `Rendered`. **This is the invariant that makes the whole prompt
bounded:** if every surface respects its budget, the sum respects the
total. Each existing prompt "part" becomes a Surface wrapping its current
builder (identity, topology, memory sections, task references), plus one
new surface (working set, §4.4).

### 4.3 The `ContextBudget` manager (`src/mylo/context/budget.py`)

Holds a single **total** derived from the active model's context window:
`total = floor(window * factor) - output_reserve`, where `factor`
(default ~0.6) and `output_reserve` are configurable. Renders surfaces in
**priority order**, passing each the *remaining* budget; unused budget
from a high-priority surface is **lent down** the chain.

Priority (must-fit at top, drop-first at bottom):

1. **Identity** (Layer 1, static) — always fits, tiny
2. **Critical memory** — pending conflicts + critical known issues
   (safety-relevant, small)
3. **Task references** (Layer 4) — when a task is detected
4. **Topology** (Layer 2) — already compact; renders top-N areas within
   its slice (areas already sorted by entity count)
5. **Working set** (§4.4) — relevance-ranked entity detail, fills
   remaining space
6. **Memory sections** — household/prefs/notes/patterns/baselines via the
   existing selector, capped
7. **Hints / proposals / cost notes** — first to go when budget is tight

Emits a `context.budget` telemetry line per turn with the per-surface
allocation (§6).

### 4.4 Working-set allocator (`src/mylo/context/working_set.py`)

Surface #5 — the one new prompt section, and the concrete form of
"bounded working set." Instead of the model always spending a tool
round-trip to learn entity details, it **pre-loads the likely-relevant
entities**, ranked and capped to the surface's budget. Scoring (highest
first):

- **Conversation match** — entity_id or friendly name appears in the
  user's message
- **Area/domain match** — message references an area ("kitchen") or
  domain ("lights") the entity belongs to
- **State recency** — recently changed (signals "in play"), when states
  are available
- **Monitored / pinned** — in `memory.monitored_entities`
- **Memory-referenced** — named in a note, known issue, or conflict

Take the top-N that fit the sub-budget; render compactly
(`entity_id · name · state · key attrs`). This is **additive
convenience, not a gate**: anything outside the set is still one query
tool away, so completeness is never traded away — we only save
round-trips on the common case, within a hard cap.

### 4.5 Tool-result bounding (`src/mylo/tools/read/*`)

Generalize what `query_entities` already half-does (limit 200 + detail
downgrade) into a uniform rule across **all** read tools: every result
carries a token budget; if it would exceed, return a **summary envelope**
— counts, a representative sample, and "narrow with these filters" —
rather than dumping rows or silently truncating. The model receives a
bounded, *actionable* result and can tell that narrowing is needed.

### 4.6 Reconciler bounding (`src/mylo/memory/reconciler.py`)

Today the reconciler has a 150K backstop that **skips the LLM merge**
when the payload is too big — at scale it silently stops learning. The
fix: the payload **always fits** the model window via **per-section token
caps with deterministic compaction**. Oversized sections (patterns,
notes, baselines) are **summarized, not dropped** — e.g.
`# +312 older patterns compacted` — so the merge still runs on a
representative payload. **Critical items (conflicts, user-confirmed
notes) are never the thing dropped.** Reuses the same `estimate_tokens`
and budgeting primitives as the prompt path. Pairs with the existing
pattern/finding caps.

### 4.7 Scale-safe HA fetching (`src/mylo/ha/ws_client.py`, `registries.py`)

HA's `entity_registry/list` returns the whole registry in one shot (no
pagination), so a large registry blows the fixed 60s timeout. Three-part
fix:

- **Bootstrap once, then maintain incrementally.** Fetch the full
  registry a single time at startup with an **adaptive timeout** sized to
  the instance (scale with expected entity count) + retry/backoff. After
  that, subscribe to `entity_registry_updated` /
  `device_registry_updated` / `area_registry_updated` and apply **deltas**
  to the warm in-memory registry — mirroring the existing `state_changed`
  subscription. `registries.py` gains apply-delta methods.
- **Never block a turn on a full refetch.** Turns read the warm registry;
  refreshes happen in the background.
- **Degrade, don't fail.** If even the bootstrap times out, serve a
  degraded topology from last-known data with a freshness note and retry
  in the background.

*(Boundary: the general reconnect/retry machinery is sub-project 2. This
spec owns only adaptive timeout sizing, killing repeated full fetches,
and incremental maintenance.)*

---

## 5. Data flow

1. **Turn start** → construct `ContextBudget` for the active model.
2. Assembler builds the ordered list of surfaces and calls
   `budget.render(surfaces)`, which renders each in priority order with
   the remaining budget. Result: an assembled prompt **guaranteed ≤
   total**.
3. The **working-set surface** reads the warm registry + recent state to
   rank and cap entities.
4. **Tool calls** during the turn return budgeted results (summary
   envelope when oversized).
5. **Registries** are maintained in the background via update events;
   assembly never triggers a full refetch.
6. **Nightly reconciler** builds its payload under the model window using
   the same estimator + per-section compaction, then merges.

---

## 6. Error handling — the degradation ladder

Every scale limit fails **visibly and safely**:

| Condition | Behavior |
|---|---|
| Prompt over budget after all trimming | Emit a visible "context trimmed" note. If the real API call *still* 400s on length, auto-retry once with a tighter budget, then degrade. |
| HA bootstrap/refresh timeout | Fall back to cached registry; topology rendered from last-known data with a freshness note; background retry. |
| Reconciler over window after compaction (near-impossible post per-section caps) | Skip the merge as today, but log loudly. **Critical items are never the thing dropped.** |
| Token estimate miss | Absorbed by the safety margin; the auto-shrink retry above is the backstop. |

Telemetry: a `context.budget` structured log per turn (total, per-surface
tokens, whether any surface was trimmed); `ha.registry.bootstrap` and
`ha.registry.delta` logs for fetch behavior; reconciler logs payload size
and compaction counts.

---

## 7. Testing strategy

"Go big" earns its keep here — the spec is only real if it's *provably*
bounded.

- **The invariant, property-tested** — generate random homes (100–10,000
  entities) → assembled prompt is **always ≤ budget**. The headline test.
- **Synthetic scale fixtures** — 5,000 entities / 100 areas / 1,000
  devices, reused across suites.
- **Budget manager units** — surface self-trimming, budget lending/
  rollover, priority order, empty-surface handling.
- **Working-set ranker** — mentioned entity ranks first; area/domain/
  monitored/memory matches; cap respected.
- **Tool-result bounding** — oversized query returns a summary envelope,
  not rows.
- **Reconciler bounding** — bloated memory fixture → payload ≤ window,
  merge runs, compaction markers present, **critical items survive**.
- **HA fetching** — simulated slow/timeout bootstrap → adaptive retry +
  cached fallback; registry-update events apply deltas **without** a
  refetch.
- **Named regression tests** — the 214K reconciler payload and the 60s
  registry timeout become permanent regression locks.

---

## 8. Configuration

New options (all with safe defaults; surfaced in `config.yaml` +
`translations/en.yaml`):

- `context_budget_factor` (default ~0.6) — fraction of the model window
  the prompt may use.
- `context_output_reserve_tokens` — headroom reserved for the response.
- `working_set_max_entities` — hard cap on pre-loaded entities.

Budgets derive from the **active model's** context window, so switching
providers/models adjusts automatically.

---

## 9. Proposed module layout

- `src/mylo/context/tokens.py` — shared `estimate_tokens`.
- `src/mylo/context/budget.py` — `Rendered`, `Surface`, `ContextBudget`.
- `src/mylo/context/working_set.py` — relevance ranker + working-set
  surface.
- `src/mylo/context/assembler.py` — refactored to drive surfaces through
  `ContextBudget` (existing parts become surfaces).
- `src/mylo/memory/reconciler.py` — per-section caps + compaction using
  the shared primitives.
- `src/mylo/ha/ws_client.py` + `registries.py` — adaptive bootstrap +
  event-driven incremental registry maintenance.
- `src/mylo/tools/read/*` — uniform tool-result budgeting.

---

## 10. Out of scope / follow-on

- **Resilience** (sub-project 2): reconnect/retry/circuit-breaker for HA
  WS + LLM providers, graceful degradation of transient failures.
- **Diagnostics** (sub-project 3): health checks, error capture,
  one-click bug report.
- **Failure-UX** (sub-project 4): clear actionable errors and safe states
  in the UI.

Each gets its own spec after this one ships.
