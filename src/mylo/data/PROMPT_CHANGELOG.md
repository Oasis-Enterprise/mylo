# Prompt Changelog

Per the implementation plan's versioning gap fix, prompt files are
first-class versioned artifacts. Every change to ``system_prompt.txt``
bumps the version in the first line of that file and adds an entry
here explaining what changed and why.

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
