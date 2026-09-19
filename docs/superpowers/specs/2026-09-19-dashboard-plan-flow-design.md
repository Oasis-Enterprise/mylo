# Dashboard Plan Flow — Design

**Status:** Approved design, ready for implementation planning
**Date:** 2026-09-19
**Part of:** the Dashboard Quality pillar. Sub-project 1 (memory cleanup +
two bug fixes) is bounded and ships first; sub-project 2 (plan flow) is the
architectural change.

---

## 1. Context

1.5.0b3 gave dashboards sound foundations: sections layout by default,
entity-reference validation, theme and custom-card discovery, and the
`ask_user` tool. Production use since then shows the gaps are in the layer
above those foundations:

- **Nothing can insert at a position.** Every `modify_dashboard` action
  appends. "Put the thermostat at the top of Climate" forces a full
  `update_view` re-emission, the main source of collateral damage. There is
  no move operation.
- **Views are addressable only by `path`, but `path` is optional on
  create.** A view created with just a title is permanently unaddressable and
  unverifiable. `update_view` can silently create duplicate paths.
- **Approval is a turn-wide boolean, not a bound plan.** Nothing hashes the
  previewed payload. Tapping an option on a question card submits through the
  same handler and sends `approved: true` if a dry-run is also pending.
- **The approval card shows two counters, not the plan.** The structural diff
  is computed and then discarded by the UI. For `replace_card` the user cannot
  see which card is being replaced.
- **Verify proves existence, not correctness.** `dashboard_loaded` checks a
  view path exists and sections have a cards list. A card that landed in the
  wrong section reports `ok: true`.
- **The prompt teaches the wrong width lever.** `column_span` widens a whole
  section; per-card width is `grid_options.columns`, which appears nowhere in
  the repo.
- **No per-card option validation.** A `tile` with no entity passes. The
  known-types list is missing most energy cards, producing spurious warnings.
- **The `update` action writes the whole dashboard verbatim** with no
  validation and no backup.

Separately, the nightly memory reconciler has failed every night since
2026-09-05 with `memory.reconciler_parse_failed`. Root cause: the call at
`memory/reconciler.py:330` caps output at 8192 tokens while the prompt asks
the model to re-emit the entire memory file. The file is longer than that, so
the response is cut mid-scalar around line 505 and the parser reports
"malformed YAML" because nothing checks `stop_reason`. The `patterns` section
is what pushes the file over the cap, and nothing user-facing reads it: it is
not injected into the prompt (`context/memory_injection.py` has no pattern
renderer), the findings detectors read `profiles.json` instead, and only the
Memory tab displays it.

A second log warning, `background_verify.failed` for `modify_automation`, is
a real defect: `modify_automation.py:201` verifies `automation.mylo_<slug>`,
built from the automation `id`, but HA names the entity after the alias slug.
The lookup can never match.

## 2. Goals

1. Mylo places cards exactly where the user asked, including at a position and
   by moving an existing card.
2. The user sees the full plan, section by section, card by card, before
   anything is written, and can send it back for changes.
3. Approval is bound to the plan the user saw. The model cannot apply
   anything else.
4. After apply, Mylo verifies the result against the plan, not against
   "does a view exist".
5. The nightly memory sync stops failing, and the dead learned-patterns
   subsystem is removed.

Non-goals: YAML-mode dashboards, inline editing inside the preview, a restore
tool for dashboard backups, generalizing plan-bound approval to automations
and scripts. All are noted as follow-ups in §8.

---

## 3. Sub-project 1 — Memory cleanup and two bug fixes (bounded)

Ships first as its own release. Independent of sub-project 2.

### 3.1 Remove learned patterns

Delete, in this order so each step leaves tests green:

1. `monitor/behavioral.py` and nightly step 3 in `monitor/scheduler.py`
   (`detect_patterns`, `enforce_pattern_cap`).
2. Pruner rules `_low_confidence_patterns`, `_stale_patterns`,
   `_excess_patterns`, `enforce_pattern_cap`, and `MAX_PATTERNS` in
   `memory/pruner.py`.
3. The pattern half of `compact_payload_sections` / `_reattach_compacted` in
   `memory/reconciler.py`, and pattern references in the reconciler prompt.
4. The `"patterns"` trigger entry in `context/selector.py:44` and its
   assertion in `tests/unit/test_context_assembler.py`.
5. Pattern count and delete endpoints in `server/routes_memory.py`.
6. `Pattern` model and `patterns` field in `memory/schema.py`. `MemoryFile`
   keeps `extra="allow"`; `MemoryStore.load()` pops a legacy `patterns` key
   before validation so the next save drops it from disk.
7. UI: the patterns section in `MemoryTab.tsx`, `MemoryPattern` in
   `types.ts`, and the pattern count in whatever status surfaces it.
8. Tests: the ~15 pattern-specific tests across `test_memory_sync.py`,
   `test_reconciler_compaction.py`, `test_memory.py`.

`monitor/transitions.py` stays; `profiles.py` folds from it. The
profiles-based findings engine (`profiles.py`, `detectors.py`,
`findings.py`) is untouched.

### 3.2 Reconciler truncation detection

In `reconciler.run_sync`, after the provider call:

- If `response.stop_reason == "max_tokens"`, log
  `memory.reconciler_truncated` (with `chars=len(raw_text)`) and return
  `ReconcileResult(updated=None, summary="reconciler output truncated at
  max_tokens; memory untouched", ...)`. Do not attempt to parse.
- Raise `max_tokens` from 8192 to 32768.
- New test in `test_reconciler_parse.py`: a fake provider returning
  `stop_reason="max_tokens"` yields `updated=None` and never calls the
  parser.

### 3.3 Automation verifier fix

Replace the slug guess in `modify_automation.py` with a verifier that scans
`get_states` for an entity whose `entity_id` starts with `automation.` and
whose `attributes.id == automation_id`. HA sets `attributes.id` on every
automation entity from the config `id`, independent of alias naming. The
verifier in `files/rollback.py` gains `automation_by_config_id_verifier(
automation_id)`; the existing `automation_loaded_verifier` stays for callers
that already know the entity id. Test: a state list with the entity under a
different slug than the id still verifies.

---

## 4. Sub-project 2 — Plan flow architecture

### 4.1 Overview

```
 model ──plan_dashboard(ops)──▶ validate ──▶ PlanStore ──▶ result{preview, plan}
                                                               │
                                                        UI DashboardPlanCard
                                                     [Reject] [Modify] [YAML] [Apply]
                                                               │ approved_plan_ids
 model ──apply_dashboard_plan(plan_id)──▶ check id ∈ approved ──▶ apply ops in memory
                                        ──▶ backup ──▶ one save ──▶ read back ──▶ verify per op
```

Two model-facing tools replace `modify_dashboard`:

| Tool | Tier | Cached | Writes |
|---|---|---|---|
| `plan_dashboard` | READ | no | nothing (stores plan in memory) |
| `apply_dashboard_plan` | MODIFY | no | one `lovelace/config/save` |

`modify_dashboard` is removed from the registry. Its pure helpers
(`_assemble_view`, `_resolve_card_target`, `_is_sections_view`,
`_count_cards`, `_view_entity_refs`) move into `mylo/dashboard/ops.py` and
are refactored into the op functions in §4.4. `rename_entities`' dashboard
cascade is untouched. `query_dashboard`, `query_dashboard_env`, and
`verify_change dashboard_loaded` remain.

New package `src/mylo/dashboard/`:

| Module | Responsibility |
|---|---|
| `plan.py` | Pydantic models: `DashboardPlan`, the `PlanOp` union, `PlanSection`, `CardFingerprint`, `PlanIssue` |
| `validate.py` | Target resolution, fingerprinting, entity refs, schema, lint → `list[PlanIssue]` |
| `ops.py` | Pure functions `apply_op(config, op, resolved) -> config` and `expected_after(op)` |
| `store.py` | `PlanStore`: in-memory, TTL 1h, capacity 50, keyed by `plan_id` |
| `backup.py` | Write pre-apply config snapshot, keep last 20 per dashboard |
| `verify.py` | Post-apply read-back checks per op |
| `card_schema.py` | Required-option table, `grid_options` shape, updated known-types list (replaces `validators/dashboard_schema.py` card-level checks; view-level checks move here too) |

### 4.2 Plan model

```python
class PlanSection(BaseModel):
    heading: str                       # executor prepends {type: heading}
    cards: list[dict[str, Any]]
    column_span: int | None = None     # 1..N widens the whole section

Position = int | Literal["start", "end"]   # default "end"
SectionRef = int | str                     # index, or heading text (exact, case-insensitive)

class CreateView(BaseModel):
    op: Literal["create_view"]
    title: str
    path: str                          # REQUIRED — slug, validated ^[a-z0-9_-]+$
    icon: str | None = None
    theme: str | None = None
    max_columns: int = 4
    layout: Literal["sections", "masonry"] = "sections"
    sections: list[PlanSection] = []   # sections layout
    cards: list[dict] = []             # masonry layout only
    position: Position = "end"

class AddSection(BaseModel):
    op: Literal["add_section"]; view_path: str; section: PlanSection; position: Position = "end"

class AddCards(BaseModel):
    op: Literal["add_cards"]; view_path: str; section: SectionRef | None = None
    cards: list[dict]; position: Position = "end"

class ReplaceCard(BaseModel):
    op: Literal["replace_card"]; view_path: str; section: SectionRef | None = None
    card_index: int; card: dict

class RemoveCard(BaseModel):
    op: Literal["remove_card"]; view_path: str; section: SectionRef | None = None; card_index: int

class MoveCard(BaseModel):
    op: Literal["move_card"]; view_path: str
    from_section: SectionRef | None = None; card_index: int
    to_section: SectionRef | None = None; position: Position = "end"

class UpdateViewMeta(BaseModel):
    op: Literal["update_view_meta"]; view_path: str
    title: str | None = None; icon: str | None = None; theme: str | None = None
    max_columns: int | None = None; new_path: str | None = None

class RemoveSection(BaseModel):
    op: Literal["remove_section"]; view_path: str; section: SectionRef

class DeleteView(BaseModel):
    op: Literal["delete_view"]; view_path: str

PlanOp = Annotated[Union[...], Field(discriminator="op")]

class PlanDashboardParams(BaseModel):
    dashboard_id: str | None = None    # url_path; null = Overview
    summary: str                       # one line, shown as the card title
    assumptions: list[str] = []        # choices the model made without asking
    operations: list[PlanOp]           # 1..40

class CardFingerprint(BaseModel):
    type: str
    entity: str | None                 # card.entity, or first of card.entities

class ResolvedTarget(BaseModel):
    op_index: int
    view_index: int
    section_index: int | None
    card_index: int | None
    fingerprint: CardFingerprint | None   # set for replace/remove/move

class PlanIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    op_index: int | None

class DashboardPlan(BaseModel):
    plan_id: str                       # 8-char random, server-generated
    dashboard_id: str | None
    summary: str
    assumptions: list[str]
    operations: list[PlanOp]
    resolved: list[ResolvedTarget]
    issues: list[PlanIssue]            # warnings only; errors never store
    created_at: datetime
    conversation_id: str
```

There is no full-dashboard replace op. That path was the least guarded and
most destructive; `delete_view` + `create_view` covers the legitimate cases.

Sections in a `CreateView` default `heading` cards: the executor emits
`{"type": "grid", "cards": [{"type": "heading", "heading": H}, *cards],
"column_span": N?}`. If the model's first card is already a `heading`, it is
not duplicated; the `heading` field wins for the text.

### 4.3 `plan_dashboard` — validation

Order, stopping at the first stage that produces an error:

1. **Params** — Pydantic. Op count 1–40. `path` slug regex on create and
   `new_path`.
2. **Fetch** current config via `lovelace/config`. Unreachable → error
   `dashboard_unavailable`.
3. **Target resolution**, in op order against a working copy that each op
   mutates, so later ops see earlier ops' effects (e.g. `create_view` then
   `add_section` to it in the same plan):
   - `view_path` must resolve; `create_view` path must not already exist;
     `new_path` must not collide. Errors: `view_not_found`, `view_exists`.
   - `SectionRef` as heading text matches the first card of each section
     when it is a heading card, case-insensitive exact. Ambiguous (two
     sections share the heading) → error `section_ambiguous`. No match →
     `section_not_found`. Index out of range → `section_index_out_of_range`.
   - Sections view with card op and no `section` → `section_required`.
     Masonry view with `section` set → `section_not_applicable`.
   - `card_index` in range → else `card_index_out_of_range`. Record the
     `CardFingerprint` of the card at that index for replace/remove/move.
   - `position` as int must be `0..len` inclusive.
4. **Entity references** — `dashboard_refs.extract_refs` over every new or
   replacement card, `validate_refs` against registries. Any invalid →
   error `invalid_entity_refs` with `did_you_mean`. `dashboard_refs` gains
   the missing Jinja functions (`has_value`, `state_translated`,
   `device_entities`, `area_entities`, `label_entities`) and recurses into
   dict-valued allowlisted keys.
5. **Card schema** (`card_schema.py`):
   - `type` present and a string.
   - `custom:*` must be in detected resources when resources are listable
     (error), warning when `None`. Unchanged behavior.
   - **Required options** per native type (error `card_missing_option`).
     Initial table:

     | type | required |
     |---|---|
     | tile, entity, sensor, gauge, thermostat, humidifier, light, media-control, weather-forecast, alarm-panel, plant-status, picture-entity, statistic, todo-list | `entity` |
     | entities, glance, history-graph, statistics-graph, logbook, calendar, picture-glance | `entities` |
     | button | `entity` or `tap_action` |
     | conditional | `conditions` and `card` |
     | vertical-stack, horizontal-stack, grid | `cards` |
     | markdown | `content` |
     | picture | `image` |
     | iframe | `url` |
     | map | `entities` or `geo_location_sources` |
     | area | `area` |

     Types not in the table are not checked for options.
   - `grid_options`, when present: `columns` is int 1–12 or `"full"`;
     `rows` is int or `"auto"`; any `min_*`/`max_*` keys are ints. Error
     `grid_options_invalid`.
   - Known-types list extended with the HA energy cards (`energy-date-selection`,
     `energy-usage-graph`, `energy-distribution`, `energy-gas-graph`,
     `energy-water-graph`, `energy-solar-graph`, `energy-sources-table`,
     `energy-grid-neutrality-gauge`, `energy-solar-consumed-gauge`,
     `energy-carbon-consumed-gauge`, `energy-self-sufficiency-gauge`,
     `energy-devices-graph`, `energy-devices-detail-graph`, `energy-sankey`),
     plus `clock`, `shopping-list`, `todo-list`, `statistic`, `heading`.
     Unknown types remain a warning. The list is verified against the HA
     frontend source at implementation time.
6. **Theme** — as today: if themes are listable and the theme is not
   present → error `theme_not_installed`.
7. **Layout lint** (warnings only, code prefix `lint_`):
   - `lint_no_heading` — a resulting section whose first card is not a heading
     (only reachable via `add_cards` at `start` or `move_card`).
   - `lint_empty_section` — a resulting section with zero non-heading cards.
   - `lint_section_too_large` — more than 10 non-heading cards.
   - `lint_duplicate_entity` — the same `entity` on two cards in one view.
   - `lint_masonry_view` — a `create_view` with `layout: masonry`.

Any error → `ToolResult.error("plan_invalid", ..., data={"issues": [...]})`;
nothing stored. The model fixes and re-plans.

Success → store the plan, return:

```json
{
  "preview": true,
  "plan_id": "k3v9x2ab",
  "plan": { ...DashboardPlan... },
  "entity_refs_validated": 23,
  "issues": [ ...warnings... ]
}
```

`preview: true` is what the UI keys on today; it stays the signal.

### 4.4 Op semantics (`ops.py`)

Every op is `apply_op(config, op, resolved) -> config` on deep copies. Pure,
no I/O, unit-tested in isolation.

- `create_view`: build the view dict; insert at `position` in `views`.
  Sections layout sets `type: sections`, `max_columns`, and never emits a
  top-level `cards` key. Masonry sets `cards` and no `sections`.
- `add_section`: build the section; insert at `position` in
  `view.sections`.
- `add_cards`: insert the list at `position` in the resolved card list.
  `position: "start"` on a section whose first card is a heading inserts at
  index 1, so the heading stays first. Explicit `0` inserts before the
  heading (and lint warns).
- `replace_card`: assert fingerprint, replace in place.
- `remove_card`: assert fingerprint, delete.
- `move_card`: assert fingerprint, pop, insert at `position` in the target
  list. Same-section moves compute the insert index after the pop.
- `update_view_meta`: set only the provided keys. `new_path` rewrites
  `path`; uniqueness was checked at plan time.
- `remove_section`, `delete_view`: delete.

Fingerprint assertion failure raises `TargetMismatch(op_index, expected,
actual)`; the executor aborts before saving.

### 4.5 `apply_dashboard_plan`

Params: `plan_id: str`. Tier MODIFY, so `Permissions.check` already requires
`user_approved`. Additionally:

1. `plan_id ∉ ctx.approved_plan_ids` → `ToolResult.error("plan_not_approved",
   "this plan was not approved by the user; present it and wait for Apply")`.
2. `PlanStore.get(plan_id)` is `None` (expired or never existed) →
   `plan_not_found`; the model re-plans.
3. `plan.conversation_id != ctx.conversation_id` → `plan_not_found`.
4. Fetch current config. Apply ops in order on a deep copy using the
   indices recorded in `plan.resolved` at plan time. Headings are not
   re-resolved; the fingerprints guard the recorded indices.
5. `TargetMismatch` → `ToolResult.error("target_changed", ..., data={
   "op_index", "expected", "actual"})`. Nothing written.
6. Write backup: `<mylo_data_dir>/dashboard_backups/<dashboard_id or
   "default">/<UTC ISO timestamp>.json` containing the pre-apply config. Keep
   the newest 20 files per dashboard. Backup write failure is logged and
   included in the result but does not block the save.
7. One `lovelace/config/save` with the final config. `CommandError` →
   `ha_error`; nothing partial, since there was one write.
8. Read back via `lovelace/config`. Run `verify.py` (§4.6).
9. Remove the plan from the store (a plan applies once). Audit entry with
   `plan_id`, op count, backup path.

Result:

```json
{
  "preview": false,
  "plan_id": "k3v9x2ab",
  "applied": 5,
  "backup": ".../dashboard_backups/default/2026-09-19T21-04-11Z.json",
  "verification": {
    "all_ok": true,
    "ops": [
      {"op_index": 0, "op": "create_view", "ok": true, "detail": "view 'kitchen' at index 3, 3 sections"},
      {"op_index": 1, "op": "add_cards", "ok": true, "detail": "3 cards at kitchen/Lights[1..3]"}
    ]
  }
}
```

### 4.6 Post-apply verification (`verify.py`)

The verdict is exact equality between the config the executor computed
and the config HA reads back after the save. HA stores storage-mode
configs verbatim, so a match proves every op landed. Ops within one plan
can supersede each other (add a card, then remove it), so per-op checks
against the final state cannot be the verdict; they run only on a
mismatch, to name the op whose target looks wrong. On a mismatch, for
each op the predicate over the read-back config is:

| op | check |
|---|---|
| create_view | view with `path` exists at the expected index; `type` and section count match; each section's first card is the heading |
| add_section | section at expected index; heading text matches; card count matches |
| add_cards | for each inserted card, the card at its expected index has the same `type` and `entity` |
| replace_card | card at index matches the new card's fingerprint |
| remove_card | card count decreased by one; card at index (if any) does not match the removed fingerprint |
| move_card | card at destination matches fingerprint; source count decreased |
| update_view_meta | each provided key equals its value on the view (at `new_path` if set) |
| remove_section / delete_view | target absent |

`all_ok` false does not roll back (the backup path is in the result); the
model reports the mismatch to the user with the backup location.

`verify_change dashboard_loaded` stays for ad-hoc checks with two fixes:
sections without a `cards` key are legal (no false "malformed" alarm), and
`wait_seconds` is ignored for `dashboard_loaded` since Lovelace saves apply
synchronously.

### 4.7 Approval binding — server

- `ToolContext` gains `approved_plan_ids: frozenset[str] = frozenset()` and
  `plans: PlanStore | None = None`.
- `routes_chat.py` reads `approved_plan_ids` (list of str, default `[]`)
  from the request body alongside `approved` and passes both into the
  per-turn `ToolContext`.
- `PlanStore` is created in `server/app.py` under a new `AppKeys.PLANS` and
  attached to the base `ToolContext`. The CLI path constructs one too.
- `ToolDefinition` gains `cacheable: bool = True`. `plan_dashboard` and
  `ask_user` set it `False`; the executor skips the read cache when it is
  `False`.

### 4.8 UI

**`DashboardPlanCard.tsx`** (new). Rendered by `App.tsx` when an approval
context's call name is `plan_dashboard`; other tools keep `ApprovalCard`.
Props: the plan, the warnings, `onApprove`, `onReject`, `onModify`.

Layout, top to bottom:

1. Header: pulsing accent dot, "DASHBOARD PLAN", the `summary`.
2. One block per operation:
   - `create_view`: view title and path as a heading; then each section as a
     bordered box: heading text in mono, cards as a wrapped row of chips.
     Each chip shows `type` and `entity` (or the heading/markdown text
     truncated to 24 chars). A chip whose `grid_options.columns` is `"full"`
     or ≥ 7 takes the full row.
   - `add_section`: "Add section *Heading* to *view* at *position*", then
     the section box.
   - `add_cards`: "Add N cards to *view* › *section* at *position*", chips.
   - `replace_card` / `remove_card` / `move_card`: "Replace/Remove/Move
     *type · entity* in *view* › *section* [index]" plus destination for
     move. The fingerprint is what makes this line specific.
   - `update_view_meta`: key → value list.
   - `remove_section` / `delete_view`: one line, in the warning color.
3. Assumptions: "Mylo assumed:" bullet list, if non-empty.
4. Warnings: lint and schema warnings, each one line.
5. Buttons: `Reject` · `Modify` · `Show YAML` · `Apply`.
   - `Modify` calls `onModify`, which sets the composer text to
     `"Change the plan: "` and focuses it. No message is sent.
   - `Show YAML` toggles a `<pre>` with the operations serialized as YAML.
   - `Apply` calls `onApprove`.

**`App.tsx` changes:**

- `handleSubmit(message, opts: { approved?: boolean; approvedPlanIds?: string[] })`.
  The `approved` flag and plan ids come only from the Apply handler.
  `QuestionCard.onSelect` calls `handleSubmit(label)` with no options. This
  closes the question-card approval leak for every tool, not only dashboards.
- `pendingApproval: boolean` becomes `pendingApproval: { planIds: string[] } | null`.
  `findApprovalContexts` collects `plan_id` from `plan_dashboard` results.
  The Apply handler sends `approved: true` and the collected ids.
- `api.ts` `SendOptions` gains `approvedPlanIds?: string[]`, sent as
  `approved_plan_ids`.

**`Composer.tsx`** exposes an imperative `setDraftAndFocus(text)` for
`Modify`.

### 4.9 Prompt and references (prompt 0.6.0)

Rewrite the "Dashboard design" block around the flow:

1. Gather: `query_dashboard_env` once; `query_entities` in bulk;
   `query_dashboard` when editing an existing view.
2. Ask: one consolidated `ask_user` when theme, style, or scope is genuinely
   open and stored preferences do not answer it. Record lasting answers with
   `memory_note(type="preference")`.
3. Plan: one `plan_dashboard` call with every operation. List every choice
   made without asking in `assumptions`. If it returns `plan_invalid`, fix and
   re-plan; never ask the user to approve a known-bad plan.
4. Wait: tell the user the plan is ready and stop. The user clicks Apply or
   Modify.
5. Apply: `apply_dashboard_plan(plan_id)` on the approval turn. Report the
   verification result. Do not call `verify_change` for dashboards.

Design rules kept: sections layout, heading per section (now automatic),
group by area or function, 4–8 cards per section, `tile` default, order by
importance. Width guidance corrected: per-card width is
`grid_options: {columns: N}` (12 per section, `"full"` for the row);
`column_span` on a `PlanSection` widens the whole section and is for
graph- or map-only sections.

The "Write flow" block adds one line: dashboards use `plan_dashboard` /
`apply_dashboard_plan` instead of `dry_run`.

`data/references/dashboard_examples.yaml` gains: a sections view with
`grid_options` on a wide card, a `conditional` card, an energy section
(`energy-date-selection` + `energy-usage-graph` + `energy-distribution`), a
`move_card` and `add_cards` at `start` example, and an `update_view_meta`
example. Existing `column_span`-on-section examples are relabeled as
"whole-section widening".

`query_dashboard` output gains, per section, `heading` (text of the first
card when it is a heading card) and per card `{index, type, entity}`, so the
model can address by heading and cite indices without a second query.

### 4.10 Error handling summary

| Situation | Where | Outcome |
|---|---|---|
| Plan has errors | plan_dashboard | `plan_invalid` with issues; nothing stored |
| Dashboard unreachable | either tool | `dashboard_unavailable` |
| Apply without approved id | apply | `plan_not_approved`; nothing read or written |
| Plan expired / other conversation | apply | `plan_not_found`; model re-plans |
| Target card changed since plan | apply | `target_changed` with expected vs actual; nothing written |
| Backup write fails | apply | logged, `backup: null` in result, save proceeds |
| Save fails | apply | `ha_error`; HA untouched |
| Read-back mismatch | apply | `verification.all_ok: false` with per-op detail; backup path in result |

### 4.11 Testing

All unit-level against the existing `_FakeClient` pattern. No e2e harness
exists; the UI is checked manually in HA.

- `test_dashboard_plan_validate.py`: every error code in §4.3 has a test;
  heading resolution (match, ambiguous, missing); fingerprints recorded;
  later ops see earlier ops (create then add_section in one plan); each
  required-option row; `grid_options` shapes; each lint rule fires and does
  not fire.
- `test_dashboard_ops.py`: every op × `start` / `end` / int position;
  heading preserved on `start`; same-section move index math; fingerprint
  mismatch raises; deep-copy purity (input config unchanged).
- `test_dashboard_apply.py`: exactly one `lovelace/config/save`; backup file
  written and rotated at 20; `plan_not_approved`, `plan_not_found`,
  conversation mismatch; `target_changed` aborts before save; verification
  passes on a faithful fake and fails on a fake that drops a card; plan
  removed from store after apply.
- `test_dashboard_verify.py`: each op's predicate against read-back fixtures.
- `test_plan_store.py`: TTL expiry, capacity eviction.
- `test_executor.py`: `cacheable=False` bypasses the read cache.
- `test_routes_chat.py`: `approved_plan_ids` reaches `ToolContext`.
- `test_dashboard_refs.py`: new Jinja functions and dict-valued keys.
- Removed: `test_modify_dashboard_sections.py` and the `modify_dashboard`
  half of `test_tools_m7b.py`, replaced by the above.

---

## 5. Data flow, one full example

User: "Add a Kitchen view with lights and climate, thermostat first in
Climate."

1. Model calls `query_dashboard_env`, `query_entities` (bulk), sees no stored
   theme preference, calls `ask_user` ("Which theme? Default / Mushroom /
   iOS"). Turn pauses.
2. User taps "Default". Request carries no approval flag.
3. Model calls `plan_dashboard` with one `create_view` (path `kitchen`, two
   sections: Lights with 4 tiles, Climate with the thermostat card first then
   2 tiles) and `assumptions: ["Included only entities in the Kitchen area"]`.
   Validation passes with no warnings; plan `k3v9x2ab` stored.
4. UI renders the plan card: two section boxes, chips, the assumption line.
   User clicks Apply. Request carries `approved: true`,
   `approved_plan_ids: ["k3v9x2ab"]`.
5. Model calls `apply_dashboard_plan("k3v9x2ab")`. Backup written, one save,
   read-back verifies the view at its index with the thermostat at
   `Climate[1]` (index 0 is the heading). Model reports success and the
   backup path.

## 6. Migration

- Existing conversations that contain `modify_dashboard` tool calls still
  render in the transcript (the UI renders unknown tool names generically).
- `context.yaml` files with a `patterns:` key load and are cleaned on the
  next save.
- No config schema change. No new add-on options.

## 7. Release sequencing

1. `1.5.0b6` — sub-project 1 (patterns removal, truncation detection,
   verifier fix). Via `scripts/release.sh` after the changelog entry.
2. `1.5.0b7` — sub-project 2. Plan: `docs/superpowers/plans/2026-09-19-dashboard-plan-flow.md`.

## 8. Follow-ups (not in this design)

- `restore_dashboard_backup` tool over the backups written in §4.5.
- Plan-bound approval for `modify_automation` / `modify_script` /
  `modify_scene` using the same `approved_plan_ids` channel.
- Inline edits in the plan card (remove chip, drag to reorder).
- Reconciler contract change: emit only changed sections instead of the whole
  file, which would remove the output-size class of failure entirely.
