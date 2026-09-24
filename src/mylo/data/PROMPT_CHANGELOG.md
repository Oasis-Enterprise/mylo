# Prompt Changelog

Per the implementation plan's versioning gap fix, prompt files are
first-class versioned artifacts. Every change to ``system_prompt.txt``
bumps the version in the first line of that file and adds an entry
here explaining what changed and why.

## 0.9.0 — 2026-09-23 (Surface)

- Tool descriptions are trimmed to mechanics only; the policy they used to
  carry moves here. Before calling query_entities, check the topology
  summary in this prompt first; for a whole-home or whole-area gather use
  detail="ids" with limit=2000 in one call.
- Dashboard gather: call query_dashboard_env once per build, not per view.
- Dashboard ask: ask one consolidated question rather than several in a row.
- Monitoring: after adding monitored entities, relay the result's note
  about the two-week learning period in your own words.
- Write flow: rename_entities is added to the tier-2 tool list, and its
  dropped "always dry_run first, read the cascade before approving"
  guidance is restored as its own bullet — the trimmed tool description
  no longer carries it.

## 0.8.0 — 2026-09-22 (Trust)

- Writes that verify in the background must be described as "applied,
  verifying" until an outcome arrives; verification outcomes injected into
  the system prompt are reported first.
- The model states the backup path after every write that reports one.
- Scoped approval: only the exact previewed calls are authorised; a
  `not_approved` result means the user deselected that change.
- The four manage_* tools now go through the same approval gate.
- New changes cannot be smuggled into an approval turn: everything must be
  previewed before the user clicks Apply, or it comes back not_approved.

## 0.7.0 — 2026-09-21

Custom card authoring. New "Custom cards" block: when a custom card is
justified, the card contract in six lines, the stage → plan → wait →
apply-cards-then-plan flow, and update-by-whole-file with a diff. Tells
the model to read an existing card with read_custom_card before
revising it, so an update starts from the current source.

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
- Entity-ID rules: the old "update_view REPLACES a whole view … dry_run"
  bullet is gone; it now points at update_view_meta for metadata and the
  add/replace/remove/move ops for contents, with delete_view + create_view
  as the only rebuild path.

## 0.5.0 — 2026-08-04

Dashboard quality pass — the prompt finally says what a good dashboard
looks like instead of only how to not break entity IDs:

- New "Dashboard design" section: query_dashboard_env first (installed
  themes + custom cards), sections layout for all new views with a
  heading card leading every section (never mushroom-title-card or
  markdown titles — they drift), tile as the default entity card,
  area-or-function grouping at 4-8 cards per section, column_span for
  wide cards, incremental build via create + add_section.
- Clarification flow: one consolidated ask_user question before
  building when theme/style/scope is genuinely open; lasting answers
  recorded via memory_note(type="preference").
- Post-apply verification via verify_change dashboard_loaded.

## 0.4.0 — 2026-06 (backfilled)

Entity-ID discipline for dashboards and automations: exact-ID-only
mandate (never "fix" registry spellings), query-before-build, bulk
gathering in one query_entities call, the invalid_entity_refs retry
loop, and update_view's complete-replacement semantics.

## 0.3.0 — 2026-05 (M9/M11, backfilled)

Monitoring system rules: manage_monitored usage, nightly baselines +
hourly anomaly detection description, candidate discovery flow, and
infrastructure-device notification suppressions. Also scenes, zones,
and query_traces debugging guidance as those tools landed.

## 0.2.0 — 2026-04-14 (M7a)

Added the write-path behavior rules:

- Documented the tier-2 dry-run-first flow. The model must always call
  tier-2 tools with `dry_run=true` first, present the preview, and wait
  for user approval (which arrives as an `approved: true` flag on the
  next request) before retrying with `dry_run=false`.
- Described tier-3 service-call expectations, including the extra care
  around locks/alarms/covers.
- Noted that if a dry_run returns schema/ref errors the model should
  fix and retry instead of asking the user to approve a broken change.

## 0.1.0 — 2026-04-13 (M4a)

Initial minimal system prompt. Covers identity, tool-use behavior, and
the five security rules from spec §5.2.

Deliberately does NOT include:
- Home topology (layer 2) — added in M4b
- Memory selection (layer 3) — added in M4b
- Task-specific reference examples (layer 4) — added in M4b

The prompt intentionally mentions that write tools are not yet available
so the model doesn't hallucinate them in its responses.
