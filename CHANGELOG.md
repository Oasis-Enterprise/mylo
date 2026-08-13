# Changelog

All notable changes to Mylo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0b5] — 2026-08-12

> ⚠️ **BETA — test at your own risk.** Continues the 1.5.0 beta. Back up your `context.yaml` before updating.

### Fixed
- **A conversation can no longer be permanently bricked by overlapping requests.** If the connection dropped mid-turn (Mylo keeps working server-side) and another message got sent while the first turn was still running, the two turns' messages interleaved in history — and every request after that failed with a 400 from the API ("tool_result must have a corresponding tool_use"). Two fixes: Mylo now refuses to start a second turn while one is running (the panel explains and waits instead), and history repair now heals the interleaved shape — including conversations already corrupted on b4, which work again after this update.

## [1.5.0b4] — 2026-08-12

> ⚠️ **BETA — test at your own risk.** Continues the 1.5.0 beta. Back up your `context.yaml` before updating.

### Changed
- **Notifications removed — findings now live entirely inside Mylo.** Mobile push and HA persistent notifications are gone (including the hourly `mobile_send_failed` log spam when a device is unreachable). Instead, the panel header shows a **findings badge** whenever something needs attention — tap it for an inline list with per-item dismiss (7-day snooze) and dismiss-all. This also closes an old gap: the catch-up banner only appeared after you'd been away 2+ hours, so frequent users never saw findings at all.
- **"Ignore this entity" filters now guard the findings list itself.** Suppressions previously only muted the push notification while the finding still landed; they now keep suppressed items out of your findings entirely.
- **Memory sync conflicts now appear as a finding** instead of a notification, so they're no longer easy to miss.
- **Config cleanup:** `notification_method`, `max_daily_notifications`, and `quiet_hours_start/end` are no longer used. Their config entries remain visible (marked deprecated, ignored) for this release only, so updating with old saved options can't fail validation — they'll be removed next release. `proactive_notifications` remains as the master switch for hourly background monitoring.
- Dismissing a finding is now always a 7-day snooze. (Previously, dismissed-but-still-unavailable entities kept re-notifying; use a suppression filter for a permanent mute.)

### Fixed
- **Nightly memory sync no longer fails on common LLM output quirks — this had been silently failing for weeks.** Empty lists where strings belong (`notes: []`) are coerced, duplicate YAML keys are tolerated (first occurrence wins), and validation failures now get repair attempts instead of sinking the whole merge.
- **Scratchpad growth is bounded.** A streak of failed syncs used to grow `scratchpad.yaml` (and the nightly LLM payload built from it) without limit. It's now capped, with overflow archived to `history/` — nothing is silently deleted.
- **Stopped the nightly pattern churn.** The pruner capped behavioral patterns at 200, then the detector re-created the same ~1000 from the unchanged transition window hours later — every night. The cap now runs after detection, so the list stays converged and the nightly "+1000 patterns compacted" warnings stop. Pattern freshness timestamps also persist on quiet nights, so live patterns no longer age spuriously toward the stale-pattern cleanup.
- **A dropped connection can no longer wipe an in-progress turn's context.** After an SSE disconnect (e.g. the add-on restarting mid-conversation), the UI's recovery polling could reload history out from under the still-running turn, leaving the model blind and burning all 25 iterations doing nothing. The polling endpoint is now read-only, and starting/clearing a conversation while a turn is running is refused instead of corrupting it.

## [1.5.0b3] — 2026-08-05

> ⚠️ **BETA — test at your own risk.** Continues the 1.5.0 beta. Back up your `context.yaml` before updating.

### Added
- **Dashboards now come out tidy — sections layout by default.** New views use Home Assistant's modern sections layout with native heading cards, so section titles stay attached to their cards instead of drifting apart the way separate title cards do in the old masonry layout. A new `add_section` operation builds views one clean section at a time, and the design guidance (group by area or function, 4–8 cards per section, tile cards by default, wide cards spanning two columns) is baked into how Mylo builds.
- **Mylo asks before it guesses.** When a dashboard request leaves real choices open — theme, style, which areas to include — Mylo asks one consolidated question with tappable option buttons right in the chat, then remembers your answer (theme and layout preferences persist across conversations).
- **Theme and custom-card awareness.** Mylo now discovers which frontend themes you actually have installed (no more guessed theme names) and which HACS custom cards are registered — it only uses custom cards that exist, falling back to native cards otherwise, so generated dashboards never render "Custom element doesn't exist".
- **Card configs are validated before you approve.** Every dashboard write is structure-checked (missing card types, uninstalled `custom:*` cards, malformed sections/stacks) and blocked with a fix list before the preview — and `verify_change` can now confirm a dashboard view actually loaded after apply.
- **Clearer approval cards for dashboard changes.** Previews now say what's being built ("Create view "Kitchen" — sections layout, 3 sections, 12 cards", entities validated, schema warnings) instead of a generic "dry run".

### Fixed
- **Entity references inside sections-layout views were never validated** — a typo'd entity in a sections view sailed through to a broken card. All nested references are now checked with did-you-mean suggestions.
- **Saved layout preferences were silently ignored.** A stored dashboard layout preference never reached the model (and could even be wiped by the nightly memory sync). It now shows up in every conversation and is protected.
- **Requests like "make my home page look nicer" are now recognized as dashboard work**, so the dashboard reference examples load for them.
- Dashboard builds have more output headroom (8k tokens), so a full view fits in one step instead of being assembled piecemeal.

## [1.5.0b2] — 2026-06-30

> ⚠️ **BETA — test at your own risk.** Continues the 1.5.0 beta. Back up your `context.yaml` before updating.

### Fixed
- **The WebSocket no longer deadlocks itself — the big one.** The connection's read loop was waiting on event handlers inline, so whenever Home Assistant fired a registry-update event, the handler's own follow-up request could never get a reply and timed out after 60 seconds — and while it was stuck, *every other* command (including saving labels or automations) timed out too. Events are now handled off the read loop, so commands flow freely. This is the root cause behind the "Mylo's WebSocket is slow" behavior on large instances.
- **Config writes during a busy moment no longer hang.** Reads briefly wait for the connection to recover (a quick reconnect is now invisible); writes fail fast with a clear "not applied, retry" instead of hanging — and if a write was sent but its confirmation was lost, Mylo says so rather than silently risking a double-apply.
- **Registry-update storms are coalesced.** A burst of registry changes collapses into a single refresh instead of one expensive refetch per event.
- **No more `Cannot write to closing transport` error spam** when you navigate away mid-response, and background verify survives a brief reconnect.

### Added
- **Connection health signal** (state, consecutive failures, last-ready, in-flight commands) logged for visibility.

## [1.5.0b1] — 2026-06-29

> ⚠️ **BETA — test at your own risk.** This is a pre-release for testing the new scale-hardening work on large instances. Back up your `context.yaml` before updating. If anything misbehaves, report it; a stable `1.5.0` will follow once it's proven out.

### Added
- **Scale hardening — Mylo now stays fast and reliable on large homes (2,000+ entities).** A single context-budget authority makes the assembled prompt provably bounded regardless of home size, so big instances stop hitting "prompt too long" and runaway costs:
  - The system prompt is assembled from priority-ordered "surfaces" within a token budget derived from the model's context window (configurable via `context_budget_factor` / `context_output_reserve_tokens`). Safety-critical conflicts are never the thing dropped under pressure.
  - A relevance-ranked **working set** pre-loads the entities most likely relevant to your message (configurable via `working_set_max_entities`), so the model needs fewer lookups — anything else is still one query away.
  - **Memory sync no longer fails on large memories.** The nightly reconciler now compacts an oversized payload to fit the window (and re-attaches what it set aside, so nothing is lost) instead of skipping the merge.
  - **Registry fetches survive big instances.** The HA registry load now scales its timeout to instance size and keeps the last-known data on a slow fetch, instead of timing out at a fixed 60s.

### Changed
- Read-tool results bound uniformly through a shared helper (true total + a "narrow your filters" hint when truncated).

## [1.4.3] — 2026-06-25

### Added
- **Monthly budget now actually does something.** `monthly_budget_usd` was a setting that did nothing. Mylo now tracks estimated spend across the calendar month (persisted, survives restarts) and gives a gentle heads-up once you pass 80% of the budget — a soft warning, **not a hard block**, so your assistant never goes dark mid-task. Resets each month. Both budget fields now have help text spelling out that they warn rather than block.

### Fixed
- **Accurate cost estimates for every provider.** Cost estimates assumed Anthropic pricing for everyone, so Gemini and OpenAI figures were wrong (roughly 10× too high for Gemini Flash) and local Ollama models showed a cost when they're free. Added current Gemini (2.5 Pro/Flash/Flash-Lite, 3 Pro) and OpenAI (GPT-5.x) pricing, and local models now correctly show **$0**.

## [1.4.2] — 2026-06-25

### Fixed
- **Config page no longer claims Anthropic-only.** The LLM Provider help text still said *"Only `anthropic` is supported in v1"* even though OpenAI, Gemini, and Ollama have all been wired up — it now lists every supported provider.
- **API key is no longer required for local models.** The key field was marked required, forcing Ollama users to enter an Anthropic key they don't need. It's now optional (leave it blank for Ollama), and the field is relabeled "LLM API Key" since it serves whichever provider you pick.

## [1.4.1] — 2026-06-24

### Fixed
- **Memory sync no longer chokes on fenced output** — the nightly reconciler occasionally wrapped its YAML in a ```` ```yaml ```` code fence without a matching close (or with trailing text), which slipped past the old strip step and broke the parser ("found character '`' that cannot start any token"), so the memory failed to sync. The fence is now stripped line-by-line and tolerates a missing or misplaced closing fence.
- **Chat no longer crashes on a corrupted conversation start** — if a stored conversation's history trimmed down to nothing (e.g. after a reset), the agent could send an empty message list and get a hard 400 ("at least one message is required"). It now falls back to your current message so the turn still goes through.

## [1.4.0] — 2026-06-15

### Changed
- **Much cheaper, faster AI on big tasks.** Large requests (e.g. "redesign my whole dashboard") used to spiral — the agent re-queried the same entities hundreds of times, burned millions of tokens, and kept stopping to ask you to continue. Reworked how the agent reuses what it has already gathered and how the conversation is cached:
  - The agent now keeps the entity IDs it looked up instead of discarding them mid-task, so it stops re-querying the same things.
  - It gathers in one broad query instead of dozens of narrow ones (and the default result size is larger).
  - The conversation history is now reused from cache across the steps of a single turn (~90% cheaper on the repeated context) instead of being re-sent at full price every step.
  - Identical repeat lookups within a turn are served from a per-turn cache, with a nudge to move on if it keeps repeating.
  - Result: far fewer tokens and dollars per task, and complex jobs finish in one turn without "continue" prompts.

### Added
- **Cost & cache telemetry** — each turn now reports an estimated USD cost and cache-hit ratio (in logs and the chat response), so usage is measurable.

## [1.3.1] — 2026-06-14

### Fixed
- **Fewer "keep going" prompts on long tasks** — the agent's per-turn step limit was raised from 8 to 25, so multi-step jobs (e.g. redesigning a dashboard with many entity lookups) finish in one turn. If the limit is still reached, the turn now ends with an explicit pause message ("reply continue and I'll pick up where I left off") instead of stopping silently.
- **Dashboard `update_view`** — guidance so the model assembles the complete replacement view before previewing, and prefers `add_cards`/`replace_card` for incremental edits. Avoids an empty-preview `missing_param` error on view rebuilds.
- **In-app version tag** — the version shown under the Mylo name now reflects the real release (it had been stuck at 1.1.7). `__version__`, `pyproject`, and `config.yaml` are kept aligned.

## [1.3.0] — 2026-06-14

### Added
- **Scenes** — `modify_scene` creates / updates / deletes / activates scenes, with a "snapshot current entity states" convenience on create.
- **Zones** — `modify_zones` creates / updates / deletes zones (latitude/longitude/radius/passive) with partial-merge updates; the built-in `home` zone is protected.
- **Automation & script trace debugging** — `query_traces` (read-only) inspects run traces to answer "why did/didn't this run": trigger, step outcomes, where it stopped, and errors.
- **More helpers** — `manage_helpers` now also manages `schedule` (weekly on/off) and `input_button` helpers.
- **Batch dry-run approvals** — when several changes are previewed in one turn, the approval card lists them all and applies them together with one click.

## [1.2.2] — 2026-06-13

### Fixed
- **Runaway behavioral patterns** — pattern detection accumulated unbounded near-duplicate patterns (10k+ on a large instance), bloating `context.yaml` until memory sync exceeded the model context window. Pattern ids are now bucketed to 30-min slots so nightly drift updates the existing pattern instead of minting a new one; the pruner drops behavioral patterns not re-confirmed in 21 days and hard-caps the list at 200; nightly sync self-heals a bloated list. Use the Memory tab's prune (or `POST /api/memory/prune`) to clear an existing backlog without the LLM.

## [1.2.1] — 2026-06-13

### Fixed
- **Memory sync on large instances** — sync failed with "prompt is too long" because the reconciler sent the entire memory file (including machine-state sections and the full new-entity list) to the model. It now sends only the sections it reconciles, carries machine-owned state over verbatim, samples the state diff, and degrades gracefully if the payload is still too large.

## [1.2.0] — 2026-06-12

### Added
- **Learned-norms monitoring** — replaces fixed-threshold alerts with per-entity learned profiles ("quiet until confident"). Duration and while-away alerts only fire when a device deviates from its own learned history, and only after enough observation (~2 weeks). Alerts explain the norm in plain English ("on for 8h — longest you've left it is 6h").
- **Per-finding dismiss** in the catch-up banner — dismiss one finding (7-day cooldown) or all at once.

### Fixed
- **The ever-growing alert list** — monitor findings are now deduplicated, capped at 5, auto-resolve when the condition clears, and expire after 48h. The legacy backlog is cleared automatically on first run after updating.
- **Sensor anomaly noise** — statistical alerts now require a stronger deviation (3.5σ) sustained across two consecutive checks; one-hour blips no longer notify. Presence is decided only from definitive person states, so a tracker outage can't trigger away-alerts.

## [1.1.7] — 2026-05-25

### Added
- **Sections-layout dashboard support** — `replace_card` / `remove_card` / `add_cards` accept `section_index` to target a specific section's cards on HA sections-layout views, avoiding full-view rewrites. `query_dashboard` reports layout type and per-section card counts.

## [1.1.6] — 2026-05-25

### Fixed
- Dashboard query results preserved across conversation compression.
- Retry on Anthropic 5xx responses, including 529 "overloaded".

## [1.1.5] — 2026-05-19

### Changed
- Automation/script reload switched to the optimistic apply + background-verify pattern for HA's slow reload operations.

## [1.1.4] — 2026-05-18

### Fixed
- Reconciler unicode stripping for LLM-emitted invisible characters.
- Suggestion suppression for infrastructure devices (network switches, cameras, servers) that are intentionally always-on.

## [1.1.3] — 2026-05-17

### Added
- Proactive suggestions contained in Mylo's catch-up banner instead of firing as standalone notifications.

## [1.1.2] — 2026-05-16

### Added
- Actionable mobile notifications (accept/reject from the notification) and a `notification_method` config option.

## [1.1.1] — 2026-05-15

### Fixed
- Internal fixes and version alignment.

## [1.1.0] — 2026-05-15

### Added
- **Helper entity creation** — create/update/delete all 7 HA helper types (input_boolean, input_number, input_select, input_text, input_datetime, timer, counter) through conversation via the `manage_helpers` tool.
- **Script management** — create/update/delete scripts stored in packages/agent.yaml via the `modify_script` tool. Same dry-run → approve → rollback flow as automations.
- **Entity history queries** — `query_history` tool retrieves state history over time (up to 1 week). Summary mode returns min/max/avg/count; raw mode returns state change list.
- **State transition logger** — subscribes to HA state_changed events and records on/off transitions for lights, switches, locks, covers, person entities in a rolling 14-day JSONL log. Feeds the pattern detector and suggestion engine.
- **Presence-aware monitoring** — hourly sweep now checks person.* entities. When nobody is home and lights/switches are on, fires a notification: "3 lights on while away."
- **Proactive suggestion engine** — detects "lights on while away", "lock unlocked >30 min", "device running >4 hours" and sends suggestions. Tracks accept/reject/ignore rates per pattern. Silences after 5 rejections; offers automation creation after 3 acceptances.
- **Automation proposals** — when a suggestion has been accepted 3+ times, Mylo proactively offers to create an automation. Builds it via modify_automation, marks the suggestion as automated so it stops firing.
- **Behavioral pattern detection** — nightly analysis of 14 days of transition data to find recurring time-of-day behaviors. Uses time-of-day clustering with midnight-wrap handling. Stores as Pattern entries in context.yaml.
- Suggestion memory schema (Suggestion model with outcome tracking).
- API endpoints: GET /api/suggestions, POST /api/suggestions/{id}/respond, POST /api/suggestions/{id}/automated.
- 24 tools now registered.

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
