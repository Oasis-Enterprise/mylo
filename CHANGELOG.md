# Changelog

All notable changes to Mylo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.6] — 2026-05-04

### Fixed
- Sync crash on notes containing colons (e.g. "Tire specs (cold): Front 36 psi"). The YAML parser now auto-quotes values with embedded colons when the initial parse fails.
- Anomaly detection: ignore tiny battery fluctuations. Added minimum stddev floor (2% of baseline mean) and minimum absolute change threshold (5 units).
- Ollama: budget warnings disabled since cost is $0.

## [1.0.5] — 2026-04-24

### Added
- Intra-turn compression: tool results compressed after each iteration, not after the whole turn. Saves 5–15K tokens per multi-tool turn.
- Prompt cache optimization: timestamp moved from system prompt to user message prefix so the system block stays stable across turns. Restores ~5,500 tokens of cache savings.
- Rate limit retry with exponential backoff (2s/5s/15s with jitter) for Anthropic 429 errors.
- History size guard: forces aggressive compression when conversation history exceeds ~12K tokens.
- Hard cap: query_entities with detail=standard and limit>100 auto-downgrades to detail=minimal.

## [1.0.4] — 2026-04-21

### Added
- Gemini provider via Google's OpenAI-compatible endpoint. Default model: gemini-2.5-flash.
- Surgical dashboard operations: update_view, replace_card, remove_card — modify single views or cards without touching the rest of the dashboard.
- ollama_url config option — set Ollama server URL from the Configuration tab.
- Cleaner anomaly notification text ("unusually low" instead of "2.6sd").
- Version tag in UI header now reads from the server instead of hardcoded.

### Fixed
- OpenAI GPT-5.x models: use max_completion_tokens instead of deprecated max_tokens.

## [1.0.3] — 2026-04-20

### Fixed
- OpenAI and Ollama provider crash: tool schemas were double-converted (KeyError: 'name'). Now stored in one canonical format.

## [1.0.2] — 2026-04-19

### Fixed
- OpenAI users with default model claude-sonnet-4-6 got errors. Auto-detects provider/model mismatches and falls back to gpt-4o.
- Better error message when no API key found for OpenAI.

### Added
- ollama_url config option for UI-based Ollama setup.

## [1.0.1] — 2026-04-19

### Fixed
- Crash-loop on large HA installs (10k+ entities) from aiohttp's 4 MiB WebSocket message cap. Thanks @weirded (#1).
- Improved WS error logging.

## [1.0.0] — 2026-04-19

### Fixed
- ApprovalCard buttons were disabled whenever the SSE stream was
  still open — the trailing "click Apply to confirm" text kept
  `sending` true and made REJECT/APPLY greyed out. REJECT is now
  always clickable (local state only); APPLY clicks during an
  in-flight stream queue a submit that fires once sending clears,
  with an "Applying…" label for feedback. No overlapping POSTs.
- `call_service` dry-run approvals now render as
  "Call domain.service on \<target\>" with a data kv line instead of
  the generic "call_service · dry run" summary. Tier label defaults
  to TIER-3.
- Root `.gitignore` `lib/` pattern silently dropped `ui/src/lib/`
  (cost + format helpers) from the repo, breaking the ui-builder
  Docker stage with "Could not resolve './lib/cost'". Negated for
  the UI path.
- Scratchpad wasn't drained after a successful sync — every sync
  re-merged the same notes and the Memory tab's Pending section
  never cleared. Drain runs only after `store.save` commits; a
  mid-save crash or malformed-YAML response preserves the
  scratchpad for retry. Archived to `history/scratchpad_<ts>.yaml`.
- Haiku reconciler output occasionally copied the scratchpad
  scope-as-dict shape (`{area: ..., entity: ...}`) into context.yaml
  Notes, failing the `scope: str | None` schema. Pydantic
  `model_validator(mode='before')` on Note lifts entity/area fields
  out and coerces scope into the expected string. Tightened
  reconciler prompt with an explicit Note shape example.

### Added
- Scratchpad pending view: Memory tab renders live
  `scratchpad.yaml` entries in a "Pending — not yet synced" section
  at the top, so notes captured in chat are visible immediately
  (they're already being used in conversations) without requiring
  a manual Sync click. Backed by `GET /api/memory/scratchpad`.

- **Milestone 12 — Activity tab + onboarding.**
  - `GET /api/activity` — paginated audit log reader with filters
    by tool name, result kind (success/failure/rolled_back/denied),
    and tier level. Reads from the existing append-only JSON Lines
    audit log via `AuditLogger.read_recent()`.
  - `ActivityTab` component replacing the placeholder. Timeline
    grouped by day ("today", "yesterday", date), each entry shows
    status dot + tool name + result/dry-run tags + timestamp + tier.
    Click-to-expand reveals full params + details JSON. Filter
    buttons: All / Success / Failures.
  - Onboarding welcome card: when no conversation history exists,
    the chat area shows a branded MYLO card with four quick-start
    suggestions (Explore, Organize, Automate, Monitor) styled with
    the Signal theme.

- **Milestone 9 + 11 — Nightly scheduler, background monitor, anomaly
  detection.** One shared `monitor.scheduler` serves both milestones.
  - `monitor.scheduler`: APScheduler AsyncIOScheduler with two job
    slots — nightly (reconciler + baseline recompute) and hourly
    (availability sweep + anomaly check). Respects `sync_frequency`
    config (nightly/weekly/manual); hourly jobs only when
    `proactive_notifications` is enabled. Starts on app startup,
    stops on cleanup. `misfire_grace_time` handles HA reboots that
    miss the scheduled window.
  - `monitor.notifier`: HA `persistent_notification.create` with
    policy enforcement — quiet hours (overnight range, e.g.
    22:00→07:00), daily cap (`max_daily_notifications`), and
    `proactive_notifications` toggle. Critical severity bypasses
    all three gates. Each notification gets a stable
    `notification_id` so HA deduplicates.
  - `monitor.hourly`: no-LLM availability sweep. Detects newly-
    unavailable entities (only monitored domains; skips disabled/
    hidden; de-duplicates across sweeps so a persistently-offline
    device doesn't re-notify). Detects stale automations (enabled
    but `last_triggered` > 48h ago). Returns finding dicts with
    id/title/message/severity for the notifier.
  - `monitor.baselines`: queries HA's
    `recorder/statistics_during_period` for monitored sensor
    entities, computes 7-day mean + stddev, stores as
    `EntityBaseline` entries in `context.yaml → baselines`. Runs
    as the second phase of the nightly job.
  - `monitor.anomaly`: z-score detection against stored baselines
    (threshold |z| > 2.5). Skips non-numeric / unavailable states.
    Severity tiers: |z| >= 4.0 = high, >= 3.0 = normal, else low.
    Runs during the hourly job when baselines exist.
  - `AppKeys.SCHEDULER` stashed on the app; `_cleanup` calls
    `stop_scheduler`.
  - 14 new unit tests covering quiet-hours logic (overnight +
    same-day), daily cap, critical bypass, unavailable-entity
    detection, stale-automation detection, de-duplication across
    sweeps, stats-point extraction, z-score spike + within-threshold
    + non-numeric skip. 326 total tests.

- **Milestone 10 — Signal theme (tactical green-on-black UI refresh).**
  Pure visual/component overhaul on top of the existing SSE, state,
  and tool-call plumbing — no behavioral changes.
  - `ui/src/styles/theme.ts` — canonical design tokens. Mirrored into
    CSS variables in `index.css`; Tailwind utilities map back via the
    extended config so both TS-inline and class-based usage share one
    source of truth.
  - JetBrains Mono + Inter shipped via `@fontsource`. Tactical mono
    for status/data/label surfaces, Inter for prose/messages.
  - New components (one file each): `Header`, `StatusDot`, `Tag`,
    `SeverityCard`, `ApprovalCard`. Primitives the rest of the UI
    composes on.
  - `Header` is two-row: dot + MYLO wordmark + version + right-aligned
    Chat/Memory/Activity tabs on top; mono 9px status bar on bottom
    with live entity/automation counts, memory-sync age (polled),
    pending-conflict callout, and session turn/token counters.
  - `Composer` now shows a mono 9px metadata row above the input
    with "budget: Nk/200k tokens · cost: $X.XX this session",
    computed from real per-turn usage via the new `store.ts` zustand
    session store. Send button is an accent-soft arrow in mono
    uppercase.
  - `ApprovalCard` replaces the bottom indigo approval bar. Pulsing
    accent dot, "AWAITING APPROVAL" header, tier tag, red/green diff
    block when a rename is pending, Reject/Apply buttons (Apply with
    accent glow).
  - `ToolCallBlock` restyled: status dot with glow, accent-colored
    tool name in mono bold, dim-mono summary, right-aligned
    duration + rotating chevron. Click-to-expand params in mono 10px.
  - Client-side tool duration tracking (`startedAt` / `durationMs`
    on `ToolCallRecord`).
  - User messages render as a right-aligned bubble with userBubble
    bg and userBorder border. Agent messages flow as plain prose —
    the asymmetry reflects that agent turns are running commentary,
    user turns are finite utterances.
  - Memory tab fully re-themed: tactical mono header, SeverityCard
    for pending conflicts (Keep A / Keep B / Dismiss), themed sync
    result card, Tag-based badges for protected notes / issue status.
  - New `GET /api/status` endpoint feeds the header — entity count,
    automation count, memory sync state, provider-presence flag.
  - `lib/format.ts` and `lib/cost.ts` helpers for relative time,
    token/dollar formatting, and per-model cost estimation (Sonnet,
    Opus, Haiku rates).
  - Keyframe `pulse-dot` (2s opacity loop) powers the AWAITING
    APPROVAL dot and typing indicator.
  - Activity tab is a placeholder until M12 lands the audit timeline.

- **Milestone 4c — Four-layer context assembler.** Replaces the
  always-inject-everything prompt with a selective, task-aware
  assembler that scales to 2000+ entity homes without ballooning
  token cost.
  - `context.topology` (Layer 2): renders a compressed YAML summary
    of the home from live registries — area → domain counts,
    integration breakdown, device counts, automation/script totals.
    Memory notes tagged with an area surface inline under that area
    ("workshop conversion in progress" under `garage:`). Sorted by
    entity density so the most important areas survive truncation.
  - `context.selector` (Layer 3): keyword-based memory section
    selection per spec §6.5. Always-on sections (household,
    preferences, current time, scratchpad) + conditional gates for
    known_issues / patterns / baselines / rejected. Pending
    conflicts always included when present.
  - `context.task_detector` (Layer 4): keyword-score classifier for
    automation / dashboard / troubleshoot / entity_management.
    Threshold-gated so casual state queries don't pull reference
    packs. Returns None when no task is confidently detected.
  - `context.references` (Layer 4): on-demand reference loader with
    user overlay — shipped examples at `src/mylo/data/references/`
    can be overridden by `{mylo_data_dir}/references/` for
    house-specific conventions.
  - Shipped starter references: automation_examples.yaml (5 common
    patterns), dashboard_examples.yaml (mushroom + mini-graph +
    conditional), common_issues.yaml (unavailable / offline /
    broken triage), naming_conventions.yaml.
  - `context.assembler.assemble_system_prompt` composes all four
    layers into a final system block. Returns an `AssembledPrompt`
    dataclass with the final text, prompt version, chosen task type,
    and selected memory sections (debug/log surface).
  - `memory_injection.build_memory_section` now accepts a `sections`
    filter; default behavior unchanged.
  - `server/routes_chat.py` and `scripts/chat.py` switched to the
    assembler.
  - 22 new unit tests covering topology counts, section selection,
    task detection, reference overlay, and end-to-end assembler
    composition. 312 total tests.
  - Conversation summarization (§6.7) intentionally deferred — the
    current trim-to-last-N policy works and bundling it here would
    have expanded scope too much.

- **Milestone 8c — Memory tab + review UI.** Closes out M8 — the user
  can now see, correct, and audit everything Mylo knows.
  - Panel now has **Chat / Memory** tabs in the header.
  - Memory tab: last-sync status, counts header, "Sync now" button,
    post-sync review card (summary + prune-candidate list + Apply-prune
    button), and sections for household, notes, known issues, patterns,
    rejections, and pending conflicts. Each item has a Delete button;
    conflicts have Keep A / Keep B / Dismiss controls. User-confirmed
    notes are badged "protected" but still manually deletable.
  - New backend endpoints: `GET /api/memory/full` (entire memory as
    JSON), `DELETE /api/memory/item` (section+id), `POST /api/memory/prune`
    (apply the pruner without running the reconciler; optional `ids`
    filter), `POST /api/memory/conflict/{id}/resolve` (choice: a/b/dismiss).
  - 8 new endpoint tests covering full-view rendering, delete
    semantics (including 400 on structural sections and 404 on unknown
    ids), prune-only with ids filter, and conflict resolution
    round-trip. 286 total tests.
  - Deferred from M8c: per-item editing (delete-and-recapture via chat
    is sufficient for the trust primitive). The nightly scheduler
    lives in M11 with `monitor.scheduler`.

- **Milestone 8b — Memory reconciler, pruner, and sync endpoint.**
  - `memory.pruner` (spec §3.4): deterministic ranked sweep that
    flags candidates for removal — expired TTL, stale unconfirmed
    observations (>90d), resolved issues, low-confidence old patterns
    (<0.5 and >60d), old rejections (>180d), and lowest-reference
    notes (budget-gated). Never auto-prunes user-confirmed notes,
    active known issues, critical items, preferences, or household.
    `apply_prune` returns a pruned MemoryFile; callers decide whether
    to save.
  - `memory.reconciler` (spec §3.5): Haiku-backed merger. Loads
    current memory + scratchpad + HA state diff, prompts Haiku to
    produce an updated YAML, validates against the schema, and
    re-protects user-confirmed sections client-side (defense in
    depth against prompt drift). Parses out code fences. Detects
    contradictions via prompt-instructed `conflicts:` entries.
    Fallback path without an API key merges scratchpad as tentative
    notes so the endpoint still works offline.
  - `POST /api/memory/sync`: manual-trigger endpoint. Returns changed
    flag, summary, conflicts_added, and prune_candidates list.
    `{"apply_prune": true}` in the body drops candidates in the same
    transaction.
  - `GET /api/memory`: lightweight status endpoint with counts for
    the future Memory tab.
  - 16 new unit tests covering the pruner matrix, reconciler with
    stubbed Haiku, malformed-YAML recovery, conflict emission,
    user-section protection, and endpoint smoke. 278 total tests.

- **Milestone 7b — Organizational & dashboard tools.**
  - `modify_areas` (tier 2): create/rename/delete areas, assign
    devices and entities between areas. Pure websocket — immediate
    effect, no file ops or reload.
  - `manage_labels` (tier 1 for list, handler-enforced approval for
    mutations): create labels with optional color, assign/remove labels
    on entities and devices. Computes label set diffs against the
    current registry before updating.
  - `modify_dashboard` (tier 2): create/update/delete Lovelace views
    and cards in storage-mode dashboards via websocket. Dry-run returns
    a structural diff; apply writes via `lovelace/config/save` —
    immediate, no reload needed. YAML-mode dashboards flow through
    write_config_file / patch_config_file.
  - `rename_entities` (tier 2): rename entity_id and/or friendly_name
    via the entity registry. Optional cascade: scans automations.yaml,
    packages/agent.yaml, and all storage-mode dashboards for the old
    entity_id, shows reference counts in dry-run, replaces on apply.
    Most impactful write tool — the dry-run preview is critical here.
  - 15 new unit tests covering all four tools: area CRUD + assignment,
    label list/create/assign, dashboard view create/delete + dry-run,
    rename with reference scanning + validation guards (nonexistent
    entity, already-exists target). 249 total tests.
  - 18 tools now registered (9 tier-1 read, 6 tier-2 write, 2 tier-3
    action, 1 hybrid).

- **Milestone 7a — Write path: tier-2 tools, rollback loop, tier-3
  service calls.** This is the big capability unlock — Mylo can now
  actually modify your HA.
  - `files.rollback.apply_with_rollback`: backup → atomic write →
    reload → verify → rollback-on-failure. Every step's outcome lives
    in `StepResult` so tool handlers can diagnose exactly what went
    wrong.
  - `write_config_file` (tier 2): generic YAML write under /config/.
    Runs schema + entity-ref + template validation before touching
    disk; on dry_run=True returns a structured diff preview; on
    dry_run=False runs the full rollback pipeline.
  - `patch_config_file` (tier 2): surgical add/update/remove at a
    dotted path — safer than rewriting a whole file.
  - `modify_automation` (tier 2): create/update/delete/enable/disable
    against a single managed package at /config/packages/agent.yaml.
    YAML-mode only in M7a; storage-mode handling lands later.
  - `call_service` (tier 3): invoke any HA service with a hard-
    blocklist (restart/shutdown/reboot) and a restricted-services
    warning set (lock/unlock, alarm_disarm, cover/open_cover).
    Rate-limited.
  - `reload_config` (tier 3): trigger a specific domain reload.
  - Permission gate now understands `dry_run` — tier-2 dry runs are
    allowed without `user_approved` so the LLM can propose → user sees
    → user approves. Non-dry-run writes still require approval.
  - Server accepts `approved: true` in the chat request body; the UI
    surfaces an Apply button after any tool call that returns
    `preview: true` in its data, and the next user message carries the
    approval flag through to the permission gate.
  - System prompt v0.2.0: documents the dry-run-first write flow and
    the tier-3 physical-action expectations. Prompt changelog updated.
  - 25 new unit tests (202 total). Rollback pipeline covered end-to-
    end including first-write rollback-by-delete, verify failure, and
    reload failure. Write tools covered through the executor so the
    permission matrix, audit log, and dry-run gate all fire as they
    would at runtime.

- **Milestone 6 — Validators, resolver, file manager.** Foundation for
  the write path — no behavior change yet, but every piece M7's write
  tools will compose against is now in place and tested.
  - `validators.yaml_parser`: ruamel round-trip that preserves comments,
    key order, and quoting; HA's `!secret` / `!include*` / `!env_var`
    / `!input` tags are preserved through load → dump so edits never
    trash a user's magic references.
  - `validators.automation_schema`: lint automation/script configs for
    the shapes the LLM most commonly gets wrong — missing
    trigger/action, invalid mode, unknown platform (warning), bad
    service-call shape, nested choose/if-then/repeat structure.
    Errors vs warnings: warnings surface in previews but don't block.
  - `validators.template_check`: parse Jinja2 ASTs, report syntax
    errors with line numbers, and extract entity refs from
    `states()` / `state_attr()` / `is_state()` / `expand()` calls.
  - `validators.entity_refs`: walks a config tree finding every
    `entity_id` / `target.entity_id` / template-extracted ref, runs
    each through the resolver, produces a structured report with
    resolutions + mismatches + template errors.
  - `resolver.resolver`: **the hallucination defense** — exact-hit
    first, rapidfuzz fuzzy match next (≥0.92 similarity, single
    candidate auto-corrects; ambiguous stays mismatch), `did_you_mean`
    suggestions on miss. Covers entity IDs, devices (by id or display
    name), and areas.
  - `resolver.errors`: the standardized envelope. Every ref failure
    emits the same `{error, invalid_ref, kind, did_you_mean, hint}`
    shape so the LLM's self-correction loop sees one schema.
  - `files.manager.atomic_write`: POSIX-atomic writes via
    sibling tempfile + fsync + `os.replace`. Readers always see the
    old file or the fully-written new one, never a torn half.
  - `files.backup`: timestamped backups under
    `{mylo_data_dir}/backups/<relpath>/<ts>.yaml`, rotated to the 10
    most recent per file. Returns a `BackupHandle` the audit log can
    record.
  - `files.diff`: structural YAML diff for dry-run previews.
    Normalizes formatting differences away so the UI shows only real
    edits — no noise from ruamel's reflow or quote-style flips.
    `None → dict` gracefully produces per-key `added` entries.
  - 42 new unit tests across six new modules. 181 total, all green.

- **Milestone 5 — Panel UI.**
  - `server.app`: aiohttp application factory that owns the HA client,
    registries, conversation store, tool registry, and provider as
    long-lived singletons.
  - `server.routes_chat`: `POST /api/chat` streams the tool loop via
    SSE (`event: text/tool_call/tool_result/done/error`);
    `POST /api/conversation/clear` resets the thread;
    `GET /api/health` for liveness.
  - `server.static`: serves built UI; falls back to a placeholder index
    when the UI isn't built (source checkout without `npm build`).
  - `ui/`: Vite + React + TypeScript + Tailwind. Zustand is a dep but
    not yet used — state lives in `App.tsx` for now. Components:
    `Message`, `ToolCallBlock` (collapsible), `Composer` (textarea with
    Enter-to-send, Shift+Enter for newline). SSE parser in `api.ts`
    since we need POST-body with a streaming response (EventSource
    can't do POST). Token usage + cache hit/miss surfaces in the
    header.
  - `scripts/dev.sh`: one command runs Python server + Vite with HMR;
    Vite proxies `/api/*` so SSE works locally.
  - Dockerfile: multi-stage — Node builds the UI, output copied into
    the Python package's static dir, runtime image stays
    Python-alpine.
  - `__main__` now starts and blocks on the server instead of
    printing config and exiting.

### Added / Changed
- **Milestone 4b — Robustness pass.** Five targeted fixes surfaced by real
  use against a 2204-entity home.
  - `ws_client.send_command` now takes a ``timeout`` kwarg (default 60s)
    and raises `CommandTimeout` instead of hanging. Tool handlers surface
    it as a structured error the model can reason about.
  - New `StatesCache` on `ToolContext` — a 30s TTL cache so multiple
    tools in one turn share a single `get_states` fetch. On 2000+ entity
    homes this cuts multi-tool turn time significantly.
  - `query_automations` fetches configs in parallel via `asyncio.gather`
    when `include_config` is true (was serial; 71 automations took ~7s).
  - `query_logs` default `hours` lowered from 24 to 6 — large windows
    shipped megabytes of logbook entries over websocket before trimming.
  - `tool_loop` yields one `TextEvent` per text block instead of
    joining with newlines; fixes a suspected truncation bug and is the
    right shape for streaming UI later.
  - Anthropic prompt caching: system prompt + tool definitions are
    marked `cache_control: ephemeral`. First call pays full price plus
    a small cache-write surcharge; every call in the 5-min window
    after reads the cached ~7KB prefix at ~10% cost. Chat CLI shows
    `cache_read` / `cache_write` counters when present.

- **Milestone 4a — LLM loop + CLI chat.**
  - `llm.provider` — minimal Provider protocol (one `message()` call
    returning a `ProviderResponse` with text + tool_calls + stop_reason +
    usage). Anthropic-shaped content blocks throughout.
  - `llm.anthropic_provider` — Claude SDK integration using
    `messages.create` (non-streaming; streaming lands with the UI in M5).
  - `llm.tool_loop.run_turn` — async iterator yielding `TextEvent` /
    `ToolCallEvent` / `ToolResultEvent` / `DoneEvent`. Handles the
    "call tools, feed results back, repeat" dance with a safety bound
    on iterations.
  - `conversation.storage` — SQLite-backed conversation log with monthly
    rollover, `user_id` and `prompt_version` columns from day one.
  - `conversation.manager` — thin in-memory wrapper that hydrates from
    storage and flushes each turn immediately.
  - `data/system_prompt.txt` (v0.1.0) and `data/PROMPT_CHANGELOG.md` —
    versioned prompt artifact per the plan's versioning fix.
  - `python -m mylo.scripts.chat` — interactive REPL. Slash commands for
    `/clear`, `/history`, `/usage`, `/quit`. Per-turn token usage
    surfaces so cost is visible.
  - Six new unit tests covering the tool loop with a scripted fake
    provider — text-only turns, tool-use turns, error-passthrough,
    max_iterations safety bound, storage hydration, usage aggregation.

- **Milestone 3 — Safety & audit.**
  - `safety.sanitizer` — prompt-injection defense with 15+ regex patterns
    and per-field length limits. Suspicious values are replaced with
    `[sanitized: injection-suspected]` markers (the LLM still knows a
    value existed) and logged for review. Structural fields like
    `entity_id` skip content scanning to avoid false positives.
  - `safety.audit` — append-only JSON Lines log at
    `{mylo_data_dir}/audit/YYYY-MM.log`. Monthly rotation, tolerant read
    (malformed lines skipped, missing files return empty), async writes
    serialized by an instance lock.
  - `safety.rate_limits` — daily and per-conversation counters. Daily
    resets on UTC midnight, per-conversation resets per `conversation_id`.
    Default caps match spec §5.5 (20 file writes / 100 service calls / 50
    renames per day; 50 tier-3 calls per conversation).
  - `safety.permissions` — tier gate enforcing:
    - tier 1 (read) always allowed;
    - tier 2 (modify) requires `user_approved`;
    - tier 3 (action) requires `user_approved` + consumes a rate-limit token.
  - `tools.executor` now runs: lookup → param validation → permission
    check → handler → audit write. Denied calls are still audited.
    Approval + dry-run signals flow through `ToolContext`.
  - New `--approve` and `--dry-run` flags on `python -m mylo.scripts.call`.
  - 49 new unit tests (125 total) — sanitizer (15 injection + 10 benign
    payloads), audit log round-trip, rate-limit matrix, permission gate
    coverage for every (tier, approval) combination.

- **Milestone 2b — Remaining tier-1 tools.**
  - `query_devices`: filter by area/manufacturer/model/integration/regex,
    optional entity listing per device, manufacturer roll-up.
  - `query_automations`: list automations/scripts/scenes with
    triggers/conditions/actions, filter by area or referenced entity,
    optional full config fetch (storage-mode only).
  - `query_dashboard`: list dashboards or return view summaries for a
    dashboard; fetch a single view's full cards; storage-mode path only
    (YAML dashboards flow through `read_config_file`).
  - `query_logs`: three unified sources — `state_history`, `logbook`,
    `system_log` — with severity / entity / hours filters; system log
    entries trimmed so tracebacks don't flood context.
  - `query_system`: `overview`, `integrations`, `addons`, `hardware`
    scopes; add-on/hardware paths gracefully error with `unavailable`
    when the Supervisor token isn't present (outside add-on).
  - `read_config_file`: YAML/text reads under `/config/` with path-
    traversal defense, directory allow/deny lists, extension allowlist,
    size cap, and `!secret` masking via `safety.secret_filter`.
  - `verify_change`: minimal `entity_exists` and `automation_loaded`
    checks; richer checks land with the rollback loop in M7.
  - `memory_note`: tier-1 stub that appends to `scratchpad.yaml` for
    M8's reconciler to pick up later.
  - New `safety/secret_filter.py` and `safety/file_access.py` — the
    first pieces of the security layer.
  - 28 new unit tests (76 total), covering each tool plus the path-
    access policy end-to-end.

- **Milestone 2a — Tool foundations + query_entities.**
  - `mylo.tools.base`: `ToolDefinition`, `ToolResult`, `Tier` enum. Pydantic
    v2 params models double as JSON Schema for both Anthropic and OpenAI
    providers (refs inlined, titles stripped).
  - `mylo.tools.registry`: registration + lookup. Test-safe `load_all()`
    uses `importlib.reload` so registry resets in tests re-trigger
    registration.
  - `mylo.tools.executor`: thin sequencer — lookup → pydantic validation →
    handler → envelope. No business logic inline (permissions/audit/rate
    limits land in M3).
  - `mylo.tools.formatters`: `shape_entity()`, `summarize_entities()`,
    brightness/color-temp translators per spec §4.10.
  - `mylo.ha.states.get_all_states()`: thin wrapper over the `get_states`
    websocket command.
  - **`query_entities`** (tier 1, fully implemented): filter by area / domain
    / device_class / integration / regex pattern / state; optional key
    attributes; limit with truncation flag; `did_you_mean` on unknown area.
  - `python -m mylo.scripts.call <tool>` CLI with dotted-path flag overlay
    and `--params` JSON for raw input.
  - 25 new unit tests (47 total): tool_base schema generation, executor
    routing and error paths, formatter translators, and query_entities
    end-to-end against an in-memory registry + stub client.

### Fixed
- Registries double-refreshed on initial attach-after-ready because both
  the explicit refresh in `attach()` and the client's `on_ready` callback
  chain fired. Added a 0.5s debounce to `Registries.refresh()`.

### Added
- **Milestone 1 — HA websocket client.**
  - `mylo.ha.ws_client`: single persistent connection with a state machine
    (DISCONNECTED → CONNECTING → AUTHING → READY), exponential-backoff
    reconnect, message ID multiplexing for request/response, event
    subscriptions that survive reconnects transparently.
  - `mylo.ha.registries`: in-memory cache of entity/device/area/label
    registries; initial fetch on ready, refetch on `*_registry_updated`
    events, index helpers (by area, by domain, unassigned detection).
  - `python -m mylo.scripts.probe`: dev CLI that prints registry counts
    from a live HA and can watch for reconnects.
  - `.env.example` + tiny stdlib `.env` loader for dev.
  - 22 unit tests covering the ws client multiplexer, auth flow, reconnect,
    subscription re-registration, and registry index helpers.
- Initial project scaffold (Milestone 0).
- Add-on manifest, Dockerfile, Python package skeleton, CI workflow.

### Changed
- Collapsed `addon/` subdirectory into the repo root. HA Supervisor uses the
  add-on directory as the Docker build context, so the Dockerfile couldn't
  reach `../src/` or `../pyproject.toml`. Single-add-on layout (`config.yaml`
  at root) is the correct pattern here. `repository.yaml` removed.
