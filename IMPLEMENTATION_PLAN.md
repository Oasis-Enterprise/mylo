# Mylo — Implementation Plan

**Status:** Draft for review. No code written yet.
**Companion to:** `MYLO_SPEC.md` v1.0

This plan translates the spec into a concrete build. It is deliberately opinionated where the spec leaves room, and explicit about open questions where it does not.

---

## 1. Project Structure

```
mylo/
├── README.md
├── LICENSE
├── CHANGELOG.md
├── IMPLEMENTATION_PLAN.md           # this file
├── MYLO_SPEC.md                     # the spec
│
# Single-add-on layout: HA Supervisor treats the repo root as the add-on
# directory and uses it as the Docker build context. Manifest + Dockerfile
# live at root so the Dockerfile can reach src/ and pyproject.toml directly.
├── config.yaml                      # add-on manifest: options schema, ingress, panel
├── Dockerfile                       # multi-stage: build UI, install python, runtime
├── build.yaml                       # HA Supervisor build metadata (arch map)
├── rootfs/                          # s6-overlay scripts
│   └── etc/services.d/mylo/
│       ├── run
│       └── finish
├── translations/en.yaml
├── icon.png                         # (add later)
├── logo.png                         # (add later)
│
├── src/mylo/                        # python package (installed in container)
│   ├── __init__.py
│   ├── __main__.py                  # entrypoint: parse options, start server + workers
│   ├── config.py                    # add-on options loader, paths, constants
│   ├── logging_setup.py
│   │
│   ├── ha/                          # Home Assistant integration layer
│   │   ├── __init__.py
│   │   ├── ws_client.py             # websocket client, auth, reconnect, msg routing
│   │   ├── rest_client.py           # Supervisor REST (logs, add-on info)
│   │   ├── registries.py            # entity/device/area/label registry cache
│   │   ├── services.py              # service call wrapper w/ rate limiting
│   │   ├── state_history.py         # history + logbook queries
│   │   └── reload.py                # domain reload + wait helpers
│   │
│   ├── tools/                       # the 13 LLM-facing tools
│   │   ├── __init__.py
│   │   ├── base.py                  # ToolDefinition, Tier, result envelope
│   │   ├── registry.py              # discovery, provider-format compilation
│   │   ├── executor.py              # routing, permission check, audit, rate limit
│   │   ├── formatters.py            # human-friendly result shaping (§4.10)
│   │   ├── read/                    # tier 1
│   │   │   ├── query_entities.py
│   │   │   ├── query_devices.py
│   │   │   ├── query_automations.py
│   │   │   ├── query_dashboard.py
│   │   │   ├── query_logs.py
│   │   │   ├── query_system.py
│   │   │   ├── read_config_file.py
│   │   │   ├── verify_change.py
│   │   │   └── memory_note.py
│   │   ├── write/                   # tier 2
│   │   │   ├── write_config_file.py
│   │   │   ├── patch_config_file.py
│   │   │   ├── rename_entities.py
│   │   │   ├── modify_dashboard.py
│   │   │   ├── modify_automation.py
│   │   │   ├── modify_areas.py
│   │   │   └── manage_labels.py
│   │   └── action/                  # tier 3
│   │       ├── call_service.py
│   │       └── reload_config.py
│   │
│   ├── safety/                      # security & validation
│   │   ├── __init__.py
│   │   ├── sanitizer.py             # prompt injection scrubbing (§5.2)
│   │   ├── secret_filter.py         # !secret placeholder replacement
│   │   ├── permissions.py           # tier enforcement, confirmation gate
│   │   ├── rate_limits.py           # daily + per-conversation counters
│   │   ├── file_access.py           # FILE_ACCESS_RULES enforcement
│   │   └── audit.py                 # append-only audit logger
│   │
│   ├── validators/
│   │   ├── __init__.py
│   │   ├── yaml_parser.py           # safe load/dump, !secret/!include aware
│   │   ├── automation_schema.py     # HA automation schema validation
│   │   ├── dashboard_schema.py
│   │   └── template_check.py        # Jinja2 parse + entity ref extraction (uses resolver)
│   │
│   ├── resolver/                    # reference resolver (hallucination defense)
│   │   ├── __init__.py
│   │   ├── catalog.py               # compact entity/device/area/service catalog for context
│   │   ├── resolver.py              # ref validation + fuzzy match w/ confidence threshold
│   │   └── errors.py                # did_you_mean error envelope (shared by all tools)
│   │
│   ├── files/
│   │   ├── __init__.py
│   │   ├── manager.py               # read/write with atomic tmp+rename
│   │   ├── backup.py                # /config/.mylo/backups/ rotation (10 per file)
│   │   ├── rollback.py              # write→reload→verify→rollback loop (§4.8)
│   │   └── diff.py                  # preview/diff generation for dry runs
│   │
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── schema.py                # pydantic models for context.yaml
│   │   ├── store.py                 # load/save, versioned history, changelog
│   │   ├── scratchpad.py            # in-session immediate memory (§3.9)
│   │   ├── pruner.py                # deterministic ranked sweep (§3.4)
│   │   ├── extractor.py             # ConversationExtractor (§3.10)
│   │   ├── reconciler.py            # sync job orchestrator (§3.5)
│   │   ├── conflict.py              # conflict detection + resolution
│   │   └── review_queue.py          # pending review items for UI
│   │
│   ├── context/
│   │   ├── __init__.py
│   │   ├── assembler.py             # assemble_prompt() (§6.8)
│   │   ├── topology.py              # scan + compress home topology (§6.4)
│   │   ├── selector.py              # keyword-based memory selection (§6.5)
│   │   ├── task_detector.py         # §6.6 keyword scoring
│   │   └── references.py            # load on-demand reference YAML
│   │
│   ├── conversation/
│   │   ├── __init__.py
│   │   ├── manager.py               # history, summarization, persistence
│   │   ├── session.py               # per-conversation state, chain counter
│   │   └── storage.py               # SQLite-backed conversation log
│   │
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── provider.py              # abstract Provider interface
│   │   ├── anthropic_provider.py    # Claude impl, tool-use loop
│   │   ├── openai_provider.py       # stubbed for v2
│   │   └── tool_loop.py             # provider-agnostic run-until-stop loop
│   │
│   ├── monitor/
│   │   ├── __init__.py
│   │   ├── scheduler.py             # APScheduler jobs (hourly, nightly)
│   │   ├── hourly.py                # availability + automation failures
│   │   ├── anomaly.py               # z-score baselines (§7.1)
│   │   ├── baselines.py             # nightly baseline recompute
│   │   └── notifier.py              # HA persistent_notification dispatch
│   │
│   ├── server/                      # web server for panel UI + API
│   │   ├── __init__.py
│   │   ├── app.py                   # aiohttp app factory
│   │   ├── auth.py                  # validate HA ingress header
│   │   ├── routes_chat.py           # POST /api/chat (SSE stream)
│   │   ├── routes_memory.py         # CRUD for memory tab
│   │   ├── routes_activity.py       # audit log viewer
│   │   ├── routes_review.py         # sync review accept/reject
│   │   └── static.py                # serve built UI
│   │
│   └── util/
│       ├── tokens.py                # tiktoken estimator
│       ├── yaml_io.py               # ruamel.yaml wrappers (preserve comments)
│       ├── async_utils.py
│       └── paths.py
│
├── ui/                              # frontend (built → src/mylo/server/static/)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── api/                     # fetch wrappers for /api/*
│       ├── components/
│       │   ├── chat/                # message list, input, tool-call renderer
│       │   ├── memory/              # section browser, edit dialogs
│       │   ├── activity/            # timeline
│       │   ├── review/              # sync review dialog
│       │   └── previews/            # dashboard ASCII, automation plain-english
│       ├── state/                   # zustand stores
│       └── styles/
│
├── data/                            # shipped-with-container reference data
│   ├── system_prompt.txt
│   ├── security_rules.txt
│   └── references/                  # seeded into /config/.mylo/references/ on first run
│       ├── automation_examples.yaml
│       ├── dashboard_examples.yaml
│       ├── template_patterns.yaml
│       ├── card_types.yaml
│       ├── trigger_types.yaml
│       ├── common_issues.yaml
│       └── naming_conventions.yaml
│
├── tests/
│   ├── unit/                        # mirrors src/mylo/ layout
│   ├── integration/                 # against a disposable HA container
│   │   ├── conftest.py              # spin up HA via docker-compose fixture
│   │   ├── test_ws_client.py
│   │   ├── test_tool_executor.py
│   │   └── test_rollback_loop.py
│   ├── fixtures/
│   │   ├── ha_snapshots/            # seeded HA config tarballs
│   │   └── yaml_samples/
│   └── e2e/                         # playwright, UI flows
│
├── scripts/
│   ├── dev_ha.sh                    # launch local HA in docker for dev
│   ├── seed_ha.py                   # populate test HA with entities
│   └── package_addon.sh
│
├── docs/                            # GitHub Pages source
│   ├── index.md
│   ├── installation.md
│   ├── configuration.md
│   └── troubleshooting.md
│
├── .github/
│   └── workflows/
│       ├── ci.yml                   # lint, typecheck, unit, integration
│       ├── release.yml              # multi-arch docker build on tag
│       └── docs.yml
│
├── pyproject.toml                   # ruff, mypy, pytest config; deps via hatch/uv
├── uv.lock                          # or poetry.lock; pinned
├── .python-version
├── .editorconfig
├── .gitignore
└── .dockerignore
```

**Why each top-level directory exists:**

- `addon/` — everything the HA Supervisor needs to build/run the container. Kept separate from `src/` so the python package is importable standalone (for unit tests and future non-addon distributions).
- `src/mylo/` — the python application. Single namespace package. Sub-packages match spec sections (`memory/` ↔ §3, `tools/` ↔ §4, `safety/` ↔ §5, `context/` ↔ §6, `monitor/` ↔ §7).
- `ui/` — frontend source. Built artifacts land in `src/mylo/server/static/` so the python server can serve them directly (no separate web server in prod).
- `data/` — static assets baked into the container: system prompt text, seeded reference YAML. Copied to `/config/.mylo/` on first run so users can edit.
- `tests/` — three layers: unit (no IO), integration (real HA container), e2e (real browser).
- `scripts/` — developer ergonomics.
- `docs/` — GitHub Pages.

---

## 2. Dependency Audit

### Python (runtime)

| Package | Use | Essential? | Footprint |
|---|---|---|---|
| `aiohttp` | HTTP server + websocket client | Essential | Small |
| `anthropic` | Claude SDK | Essential | Small |
| `pydantic` v2 | Schema validation for context.yaml + tool params | Essential | Medium (compiled) |
| `ruamel.yaml` | YAML round-trip preserving comments/order for config edits | Essential | Small |
| `voluptuous` | HA uses it; we use it to validate against HA schemas | Essential | Small |
| `jinja2` | Parse/validate Jinja2 templates in automations | Essential | Small |
| `apscheduler` | Hourly + nightly jobs | Essential | Small |
| `aiosqlite` | Conversation history persistence | Essential | Tiny |
| `tiktoken` | Token counting for budget management | Essential | **~30MB** (bundled tokenizer data) — flag |
| `structlog` | Structured logging for audit + debug | Essential | Tiny |
| `python-slugify` | Entity ID / area ID generation | Essential | Tiny |
| `rapidfuzz` | Fuzzy match for reference resolver (hallucination defense) | Essential | ~200KB |
| `httpx` | Outbound HTTP (LLM fallbacks, Supervisor REST) — or drop if `aiohttp` covers all | Optional | Small — *drop, use aiohttp* |
| `openai` | Provider abstraction v2 | Optional | Defer to v2 |
| `watchdog` | Detect external edits to `.mylo/` files | Optional | Small |

**Flagged:** `tiktoken` adds ~30MB. Alternative: a char-count heuristic (`len(text)//4`) that's good enough for budget decisions. **Decision needed** — see open questions.

**Python stdlib covers:** `asyncio`, `dataclasses`, `pathlib`, `re`, `json`, `hashlib`, `shutil`, `difflib`, `sqlite3`.

### Python (dev only, not shipped)

`pytest`, `pytest-asyncio`, `pytest-aiohttp`, `ruff`, `mypy`, `hypothesis`, `playwright`, `uv` (dependency manager).

### Frontend

| Package | Use | Essential? |
|---|---|---|
| `react` + `react-dom` | UI framework | Essential |
| `typescript` + `vite` | Build tooling | Essential |
| `zustand` | Lightweight state | Essential (Redux is overkill) |
| `react-markdown` + `remark-gfm` | Render agent messages | Essential |
| `@tanstack/react-query` | API data fetching/caching for memory + activity tabs | Essential |
| Tailwind CSS | Styling | Essential (no component library needed) |
| `@microsoft/fetch-event-source` | SSE streaming from chat endpoint | Essential |
| `codemirror` | YAML preview/edit in dashboard preview | Optional (can defer) |

**No component library** (MUI/Chakra) — bloats bundle and we control the full visual design. Target gzipped JS bundle: <150KB.

### Container base

- `ghcr.io/home-assistant/aarch64-base-python:3.12-alpine` (and arch variants) — provides `s6-overlay`, `bashio`, nginx-capable. Alpine keeps image <200MB.

---

## 3. Module Breakdown

Format: **`module` (spec §) — responsibility; inbound calls; outbound calls.**

### Infrastructure

**`ha.ws_client`** (§2.1, §4.1) — Single persistent websocket to HA core. Handles auth (long-lived token in dev / `SUPERVISOR_TOKEN` in prod), reconnect w/ exponential backoff, message ID multiplexing, subscription streams. Exposes `async call(type, **kwargs)`, `async subscribe(event_type)`, and registry sync helpers.
- **Called by:** `ha.registries`, `ha.services`, all read tools, `monitor.hourly`, `context.topology`.
- **Calls:** HA websocket API.

**`ha.registries`** — In-memory cache of entity/device/area/label registries with live invalidation from `registry_updated` events.
- **Called by:** every tool that needs to resolve/validate references; `validators.entity_refs`.
- **Calls:** `ha.ws_client`.

**`safety.sanitizer`** (§5.2) — `ContextSanitizer` per spec. Regex injection detection, field truncation, logging.
- **Called by:** all data entering LLM context (registries output, file read output, memory load).

**`safety.audit`** (§5.6) — Append-only `AuditEntry` log. Locked to append mode; never exposes delete.
- **Called by:** `tools.executor` (every tool call), `files.rollback`, `memory.reconciler`.

**`safety.permissions`** (§4.2, §5.5) — Tier gate. Checks tier, confirmation state, rate limits, chain counter, error backoff. Can block a call pre-execution.
- **Called by:** `tools.executor`.
- **Calls:** `safety.rate_limits`, `conversation.session` (for chain counter).

### Tools layer

**`tools.base`** — `ToolDefinition`, `ToolResult`, `Tier` enum, `to_anthropic()`/`to_openai()` compile methods (§4.12).

**`tools.registry`** — Discovers tools, compiles provider-format definitions, holds the param schemas.

**`tools.executor`** (§4.1) — The single entry point when the LLM invokes a tool. Pipeline:
1. Look up tool → validate params against schema.
2. `safety.permissions.check()` — tier, rate, confirmation.
3. If tier ≥ 2 and not `dry_run=False with prior approval in session`: enforce dry-run-first flow.
4. Execute tool handler.
5. `formatters.shape()` the result.
6. `safety.audit.write()`.
- **Called by:** `llm.tool_loop`.

**Individual tool modules** — thin handlers that pull in dependencies: read tools call `ha.*`; write tools call `files.manager`, `validators.*`, `files.rollback`; `memory_note` calls `memory.scratchpad`.

### Memory layer

**`memory.store`** (§3.2, §3.8) — Load/save `context.yaml` with ruamel (preserves comments). Maintains `history/` snapshots and appends to `changelog.yaml`. Enforces schema via `memory.schema`.

**`memory.scratchpad`** (§3.9) — Fast-path immediate notes. Writes to `scratchpad.yaml`. Read by `context.assembler` and drained by `memory.reconciler`.

**`memory.pruner`** (§3.4) — Deterministic ranked sweep. Returns candidate list, never auto-deletes without user approval for non-expired items.

**`memory.extractor`** (§3.10) — Runs per conversation turn. Outputs structured extracts to a queue file.

**`memory.reconciler`** (§3.5) — Nightly orchestrator. Collects scratchpad + extracts + HA state diff → builds reconciliation prompt → calls LLM (Haiku) → produces updated YAML + changelog → queues review.
- **Calls:** `llm.provider`, `memory.store`, `ha.registries`.

**`memory.conflict`** (§3.7) — Detects contradictions during reconciliation; never silently overwrites.

### Context layer

**`context.topology`** (§6.4) — Scans HA, compresses to ~400 tokens, caches to `topology_cache.yaml`. Refreshed on sync, not per conversation.

**`context.selector`** (§6.5) — Keyword-based memory section selection.

**`context.task_detector`** (§6.6) — Keyword scoring.

**`context.references`** (§6.6, §10) — Load on-demand reference YAML from `/config/.mylo/references/`.

**`context.assembler`** (§6.8) — `assemble_prompt()`. The canonical consumer of all memory/context/topology data. Returns `{system, messages, tools}` ready for the LLM.

### Conversation layer

**`conversation.session`** — Per-conversation mutable state: chain counter, approved tool signatures for this session, error count.

**`conversation.manager`** (§6.7) — Rolling window + rule-based summarization. Persists to SQLite via `conversation.storage`.

### LLM layer

**`llm.provider`** — Abstract base: `async stream(system, messages, tools) -> AsyncIterator[Event]`.

**`llm.anthropic_provider`** — Claude impl. Uses Anthropic SDK with tool use + streaming.

**`llm.tool_loop`** — The run loop: stream → on `tool_use` event, call `tools.executor`, feed result back, repeat until `end_turn`. Emits SSE events to the server for the UI.

### Monitor layer

**`monitor.scheduler`** — APScheduler wiring; jobs defined in hourly/nightly modules.

**`monitor.hourly`** (§7.1) — Availability deltas, automation failure scan, cheap anomaly check. **No LLM calls.**

**`monitor.anomaly`** — Z-score against baselines.

**`monitor.baselines`** — Nightly recompute from HA long-term statistics (`recorder` integration).

**`monitor.notifier`** — Proactive notification dispatch; respects quiet hours, daily cap, priority rules.

### Server layer

**`server.app`** — aiohttp app. Registers routes, attaches long-lived singletons (ws_client, memory store, conversation manager).

**`server.auth`** — HA ingress provides `X-Ingress-Path` + upstream auth; validate and reject direct exposure.

**`server.routes_chat`** — `POST /api/chat`: accepts message, returns SSE stream of agent events (text deltas, tool calls, tool results, final message).

**`server.routes_memory`** — GET/PATCH/DELETE memory items for the Memory tab (§7.2).

**`server.routes_activity`** — Read-only audit log paginator.

**`server.routes_review`** — Accept/reject items from sync review queue.

### Frontend

**`ui/components/chat`** — Message list (virtualized), input with slash-command hinting, tool-call renderer (collapsed by default, expandable to see params/results).

**`ui/components/previews`** — Dashboard ASCII renderer (§7.3), automation plain-English renderer.

**`ui/components/memory`** — Section browser, per-item edit/delete.

**`ui/components/activity`** — Timeline.

**`ui/components/review`** — Sync review dialog with accept-all/reject-individually.

**`ui/state`** — zustand stores: `chatStore` (current conversation + streaming state), `memoryStore`, `activityStore`. React Query handles server cache.

### Data flow summary

```
UI → POST /api/chat → server.routes_chat
  → conversation.manager.add_turn(user)
  → context.assembler.assemble_prompt()
      ├── context.topology (cached)
      ├── memory.store.load() → safety.sanitizer
      ├── context.selector + context.task_detector
      └── conversation.manager.get_messages()
  → llm.tool_loop.run()
      ├── llm.anthropic_provider.stream() ←→ Claude API
      ├── on tool_use: tools.executor
      │     ├── safety.permissions (block or require confirm via SSE)
      │     ├── [if tier≥2 and not dry_run]: dry run first, await UI confirm
      │     ├── tool handler → ha.* or files.*
      │     ├── files.rollback loop (if write+reload)
      │     ├── safety.audit.write()
      │     └── tools.formatters.shape()
      └── emit SSE events → UI
  → memory.extractor.extract_from_turn() → scratchpad/queue
  → conversation.manager.add_turn(assistant)

Scheduler:
  hourly → monitor.hourly → monitor.notifier
  nightly → monitor.baselines + memory.reconciler → review queue
```

---

## 4. Build Sequence

Each milestone is independently runnable against a real HA instance. "Done" means the demo bullet is reproducible from a clean checkout.

### Current Status (2026-04-15)

**Shipped:**
- M0 Repo scaffold
- M1 HA websocket client
- M2a/2b Tier-1 tools
- M3 Safety & audit
- M4a LLM loop + CLI chat
- M4b Robustness pass (ws timeout, states cache, parallel auto fetch, prompt caching)
- M4c Four-layer context assembler (topology, selector, task detector, references)
- M5 Panel UI + SSE
- M6 YAML validators, reference resolver, file manager
- M7a Write path (tier-2 tools + rollback loop + tier-3 call_service + reload_config)
- M7b Organizational tools (modify_areas, manage_labels, modify_dashboard, rename_entities)
- M8a Memory foundation (schema, store, scratchpad, injection)
- M8b Memory sync engine (pruner, reconciler, `/api/memory/sync`)
- M8c Memory tab + review UI
- M10 Signal theme (tactical UI refresh — moved up the queue per user ask)

**Remaining:**
- **M4c follow-ups:** conversation summarization (§6.7) — trim-to-last-N still in use
- **M9** Nightly auto-sync: `monitor.scheduler` (APScheduler) runs reconciler on configured frequency. Manual trigger already works; this automates.
- **M9+M10 dovetail:** chain-call checkpoint (pause after N tool calls vs silent max_iterations), daily rate counters surface in UI.
- **M11** Background monitor: `monitor.hourly`, `monitor.baselines`, `monitor.anomaly`, `monitor.notifier` + proactive HA notifications. Shares scheduler with M9.
- **M12** Activity tab + onboarding (cold-start flow §6.9, quick-wins).
- **M13** HACS / add-on release: multi-arch build workflow, `repository.yaml`, docs on GitHub Pages.

Test count: 312 passing. Live-tested against a 2200-entity HA instance.

---

### Milestone 0 — Repo scaffold (0.5 day)
- `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` lint + typecheck.
- Empty `src/mylo/__main__.py` that prints config and exits.
- `addon/config.yaml` + `addon/Dockerfile` that builds and HA Supervisor will install.
- **Done:** Add-on installs from local repo in HA dev instance and shows version in logs.

### Milestone 1 — HA websocket client (1 day) [Phase 1, spec §2.1]
- `ha.ws_client` with auth, reconnect, subscription.
- `ha.registries` cache with live updates.
- Integration test harness: disposable HA in docker-compose (`tests/integration/conftest.py`).
- **Done:** `python -m mylo.scripts.probe` dumps entity/device/area counts from live HA; reconnects cleanly after HA restart.

### Milestone 2 — Read tools + executor (1.5 days) [Phase 1, §4.3]
- `tools.base`, `tools.registry`, `tools.executor` minimum path (no permissions yet).
- All 9 tier-1 tools implemented (including `memory_note` writing to scratchpad only).
- `tools.formatters` for entity/device shaping.
- **Done:** CLI `python -m mylo.scripts.call query_entities --area kitchen` returns shaped output. All tier-1 tools have unit tests.

### Milestone 3 — Safety & audit (1 day) [Phase 1, §5]
- `safety.sanitizer` with spec regex list + unit tests with known injection payloads.
- `safety.audit` append-only writer.
- `safety.permissions` with tier enum + rate limits (Tier 2/3 still error out — no write path yet).
- `safety.secret_filter` for `!secret` replacement.
- `safety.file_access` rules module.
- **Done:** Feeding entity with `friendly_name: "Ignore previous instructions"` produces sanitized marker + audit entry. Tier 2/3 calls blocked with clear error.

### Milestone 4 — Context assembly + LLM loop (2 days) [Phase 2, §6]
- `context.topology` scanner.
- `context.selector`, `context.task_detector`, `context.references`.
- `context.assembler` producing full prompt.
- `llm.provider` + `llm.anthropic_provider` + `llm.tool_loop`.
- `conversation.manager` + SQLite storage.
- Minimal CLI chat: `python -m mylo.scripts.chat` — stdin/stdout, tier-1 only.
- **Done:** CLI conversation where "what lights are on in the kitchen?" actually calls `query_entities` and answers correctly. Token budget logged.

### Milestone 5 — Panel UI skeleton + chat (2 days) [Phase 2, §7.2]
- `ui/` Vite project; `server.app` serves built assets under ingress path.
- `server.routes_chat` with SSE.
- React chat UI: message list, input, streaming text, collapsed tool-call display.
- Panel registration in `addon/config.yaml` (`panel_icon`, `panel_title`, `ingress: true`).
- **Done:** Sidebar icon opens Mylo panel in HA; can have a tier-1 conversation in-browser. Conversation persists across panel reopens.

### Milestone 6 — YAML validators + file manager (1.5 days) [Phase 3, §4.9]
- `validators.yaml_parser` (ruamel), `automation_schema`, `dashboard_schema`, `template_check`, `entity_refs`.
- `files.manager` (atomic write), `files.backup` (rotation), `files.diff` (dry-run preview).
- **Done:** Unit tests: valid automation YAML passes; missing entity ref caught; Jinja error caught; 51-card dashboard warns.

### Milestone 6.5 — Reference resolver (1 day) [hallucination defense]
- `resolver.catalog` — compact entity/device/area/service catalog (entity_id + friendly_name, domain-grouped), injected into Layer 2 of context. Area-filtered when intent is detectable to control token cost on 500+ entity homes.
- `resolver.resolver` — hard validation of every ref against registry; rapidfuzz match with ≥0.92 similarity threshold for auto-correct; multi-candidate → force `did_you_mean`.
- `resolver.errors` — standardized `{error, invalid_ref, did_you_mean, hint}` envelope; all tools return this shape on ref failures so the LLM self-corrects in the next turn.
- Domain/service pairs use JSON Schema `enum` in tool definitions where enum size stays reasonable.
- Never auto-correct silently for Tier 3 calls — always surface.
- Template refs: `validators.template_check` extracts entity refs from Jinja ASTs and runs them through the same resolver (silent failure class otherwise).
- **Done:** Unit tests: hallucinated `sensor.kitchen_temp` resolves to `sensor.kitchen_temperature` with one retry loop; ambiguous fuzzy match returns `did_you_mean` list; Tier 3 `call_service` with mistyped entity never auto-corrects.

### Milestone 7 — Write tools + rollback loop (2 days) [Phase 3, §4.4, §4.7, §4.8]
- All tier-2 tools (`write_config_file`, `patch_config_file`, `rename_entities`, `modify_dashboard`, `modify_automation`, `modify_areas`, `manage_labels`).
- `files.rollback` write-reload-verify-rollback loop.
- Dry-run/confirmation round-trip through the UI (SSE event → React approve modal → continuation).
- **Done:** From panel: "create an automation that turns off the kitchen lights at 11pm" → preview → approve → automation loads in HA; intentionally-broken YAML rolls back automatically with diagnostic.

### Milestone 8 — Memory system (2 days) [Phase 3, §3]
- `memory.schema` pydantic models.
- `memory.store` with history + changelog.
- `memory.scratchpad`, `memory.extractor`, `memory.pruner`.
- Wire `memory_note` tool to persist through scratchpad → store.
- Memory tab UI (browse/edit/delete).
- **Done:** Say "remember the basement motion sensor is unreliable" → appears in Memory tab → referenced in later conversation about basement.

### Milestone 9 — Sync job + review (1.5 days) [Phase 3, §3.5–3.7]
- `memory.reconciler` w/ Haiku call.
- `memory.conflict` detection.
- `monitor.scheduler` running nightly job.
- Review UI and `server.routes_review`.
- **Done:** Manual-trigger sync button produces changelog and review list; accept/reject applies correctly.

### Milestone 10 — Tier 3 tools + chain checkpoint (1 day) [Phase 3, §4.5, §5.5]
- `call_service`, `reload_config` with `blocked_services` + `restricted_services` extra confirmation.
- Chain limit checkpoint after 8 tool calls.
- Daily rate counters.
- **Done:** "Unlock the front door" triggers extra-explicit confirmation; blocked services refuse cleanly.

### Milestone 11 — Background monitor + anomaly (1.5 days) [Phase 4, §7.1]
- `monitor.hourly`, `monitor.baselines`, `monitor.anomaly`, `monitor.notifier`.
- Proactive notification → panel deep-link.
- **Done:** Force an anomaly in test HA (spike energy sensor) → persistent notification appears → clicking it opens panel with pre-seeded conversation context.

### Milestone 12 — Activity tab + polish (1 day) [Phase 5]
- `server.routes_activity` + Activity tab UI.
- Onboarding/cold-start flow (§6.9).
- Quick-wins suggestions.
- **Done:** Fresh install with empty memory shows discovery message; Activity tab shows week of actions.

### Milestone 13 — HACS release (0.5 day)
- Multi-arch build workflow.
- `repository.yaml`, docs on GitHub Pages.
- **Done:** Add-on installable via HACS from a test repo.

**Total rough estimate:** ~17–20 focused days. Quality-first, so real schedule will be longer.

---

## 5. Risk Register

### R1 — HA websocket auth lifecycle and edge cases
**Why hard:** Add-ons use `SUPERVISOR_TOKEN`; dev uses long-lived tokens. HA restarts drop the socket. Subscriptions need re-registration. Registry events can arrive before we've initialized caches. Auth-expired is silent until the next call.
**Approach:** Single reconnect state machine with explicit states (`disconnected`/`authing`/`syncing`/`ready`); all callers `await ready()`. After reconnect, reload registries before accepting tool calls. Fuzz test by killing/restarting the HA container in integration tests.

### R2 — YAML generation quality (Jinja2 inside YAML strings) & entity hallucination
**Why hard:** The LLM must output YAML where strings contain Jinja2, which uses `{{ }}` — itself valid YAML flow syntax. Indentation + quoting is fragile. Schema validators don't catch semantic template errors. Separately, entity_id hallucination (`sensor.kitchen_temp` when the real one is `sensor.kitchen_temperature`) is the most common first-try failure mode.
**Approach:**
- Reference examples over instructions (spec §6.6); ruamel round-trip + voluptuous schema validation; explicit `jinja2.Environment().parse()` before write.
- **Reference resolver (Milestone 6.5)** as a first-class defense: compact entity catalog primed into context; hard registry validation on every ref; rapidfuzz auto-correct at ≥0.92 similarity for Tier 1/2 (never Tier 3); standardized `did_you_mean` error envelope fed back as a tool result so the model self-corrects in the next turn without user-visible failure.
- Template refs extracted from Jinja ASTs run through the same resolver — closes the silent-failure class where entity refs hide inside `{{ states('...') }}` strings.
- Domain/service enums in JSON schemas eliminate a whole class of service-call hallucinations at generation time.
- Target: reduce user-visible hallucination failures to near zero; expect silent in-loop retries to handle the rest.

### R3 — Write→reload→verify timing reliability
**Why hard:** `homeassistant.reload_*` is fire-and-forget. No completion event. A 5-second wait is arbitrary — slow Pi 4 may need longer. "Success" criteria differ per domain.
**Approach:** Domain-specific verifiers (§4.8). Instead of fixed wait, poll: subscribe to `component_loaded`/`automation_reloaded` events where available; for others, poll entity registry until target entity appears or a timeout with exponential check. Configurable base timeout with sensible Pi 4 default (10s).

### R4 — Context window management at 500+ entities
**Why hard:** Naïve topology dump balloons. Memory selection may be too broad. Reference examples compete for tokens.
**Approach:** Topology compression is aggressive (counts + areas, no per-entity list by default). The `query_entities` tool is the escape hatch — model asks when it needs detail. Hard token budget enforced per layer in `context.assembler`; overflow strategy = truncate lowest-priority layer first (task context → memory sections by recency → topology). Tiktoken (or heuristic) counter gates this. Log budget usage per turn to tune.

### R5 — Dual-mode config complexity
**Why hard:** Storage-mode items live in `.storage/` JSON (don't touch directly); same logical object in YAML lives in `/config/` files with different schema. Tools must route correctly.
**Approach:** Tool handlers route by source. Every "modify" tool first classifies the target (storage vs YAML) via registry lookup. Storage = websocket API (`config/automation/config` etc.). YAML = file path lookup via `!include` resolution + agent workspace. Encapsulate the routing decision in a `ConfigLocator` helper used by all modify tools so the branching is one place, not per tool.

### R6 — Add-on Docker packaging + Supervisor integration
**Why hard:** Multi-arch builds, s6-overlay init, ingress routing, option schema validation. Small mistakes = add-on won't install or panel won't load.
**Approach:** Start with the official HA add-on template and bisect toward our needs. Keep `addon/config.yaml` minimal initially; add options incrementally. Test on actual Supervisor (not just docker-compose) before each milestone that touches add-on config. Use the `bashio` helpers for option reading rather than rolling our own parser.

### R7 — Chat UI state and conversation persistence
**Why hard:** SSE streams + tool-call round-trips + approval dialogs + resumption after panel close are a state-machine tangle. Ingress can drop connections.
**Approach:** SQLite as source of truth. UI is a thin view — on panel open it loads the current conversation from the server, then subscribes to new events. All "state" lives server-side; client reconciles. Tool approvals are explicit server-side pending states (not just UI modal) so a reloaded panel resumes mid-approval correctly. zustand store mirrors but doesn't own.

### R8 — Prompt injection via entity data
Covered by `safety.sanitizer`, but regex-based defense has known false-negative surface. Mitigation: security rules in system prompt (§5.2) + tier enforcement that refuses to bypass confirmation regardless of conversation content. Also, log all injection detections for user review in sync digest — turns a defense into a feature.

### R9 — Reconciliation prompt reliability
**Why hard:** Ask Haiku to edit YAML and produce strict schema output — models regularly drift.
**Approach:** Always validate reconciler output against `memory.schema` before accepting. On failure, retry with the validation error as feedback. On repeated failure, surface to user as "sync failed, review manually" rather than silently corrupting memory. Keep the previous version intact until the new one validates.

---

## 6. Open Questions — Resolved

All 14 items resolved by user. Decisions now binding on implementation.

1. **Model IDs** — `claude-sonnet-4-6` (conversation), `claude-haiku-4-5-20251001` (reconciliation). Both exposed as add-on options for user override.
2. **Token counting** — Char heuristic: `len(text) // 4`, with a 10% safety margin on budget ceilings. No tiktoken dependency.
3. **Conversation persistence** — Single continuous thread per user. Summarization manages length. No multi-thread UI in v1.
4. **Ingress only** — v1 is ingress-only, no external port. Mobile push links route back through the HA companion app.
5. **Multi-user** — Attach HA username to conversation turns and memory note metadata now (not used for scoping in v1, but avoids a migration later).
6. **Scratchpad lifetime** — Drained by the reconciliation run, not per-session. Accumulates across conversations until sync.
7. **Reference YAML ownership** — Shipped references stay read-only in the container. A separate user-writable override directory (`/config/.mylo/references/`) takes precedence. Never overwrite user edits.
8. **Sync on first install** — Immediate topology scan only. No LLM reconciliation until the first scheduled sync after the user has actually had conversations.
9. **Storage-mode patching** — `patch_config_file` rejects storage-mode targets and the tool description explicitly routes the LLM to `modify_automation` / `modify_dashboard`.
10. **Missing API key** — Panel loads; Memory + Activity tabs usable; chat input disabled with an inline banner and a link to the Anthropic key page.
11. **Distribution** — Mylo ships as an **HA add-on repository** (user adds GitHub URL in Supervisor), not via HACS. Spec §1.4 needs correction. HACS only enters the picture if we later add a companion custom integration.
12. **Mobile push fallback** — If no mobile notification service is registered, fall back to `persistent_notification` and log a one-time suggestion to install the companion app.
13. **Anomaly baseline source** — Recorder long-term statistics is primary. For entities not in LTS, fall back to state history polling **only for entities the user has explicitly asked Mylo to monitor**. No blanket polling.
14. **Conflict UI flow** — Injected as the first assistant message when the user next opens the chat panel. If ignored, stays in pending conflicts and re-surfaces at a configurable interval.

### Implementation consequences of these decisions

- `util/tokens.py` becomes trivial (char heuristic); remove tiktoken from deps.
- Drop `httpx` from deps (aiohttp covers all outbound HTTP).
- Add `user_id` field to `conversation.storage` schema and to memory note metadata from day one.
- `context.references` reads from user override dir first, falls back to shipped `data/references/` baked into the image.
- `server.routes_review` no longer handles conflict prompts — conflicts are queued into the next-conversation injection path in `conversation.manager`.
- Add a `monitored_entities` list to memory schema for the anomaly fallback rule.
- Cold-start flow (Milestone 12) explicitly does NOT call the reconciler.

---

## 8. Revisions From Feedback

Captured from the first review pass so they don't get lost.

### Structure & deps
- Dockerfile runs `pnpm build` (or `npm run build`) in a UI build stage; output copied into `src/mylo/server/static/` for the runtime stage. Dev workflow: `scripts/dev.sh` runs Vite in watch mode alongside the Python server (aiohttp proxies unknown routes to `localhost:5173` in dev).
- Use ruamel.yaml's structured diff for Tier-2 dry-run previews instead of stringwise `difflib`. Better previews for the user, fewer false "changes" from formatting drift. `files.diff` becomes a thin wrapper over ruamel round-trip + structural comparison.

### Module design
- **`tools.executor` must stay a sequencer, not a god class.** Concrete rule: it may not contain validation logic, permission logic, formatting logic, or audit logic inline — only calls into `validators.*`, `safety.permissions`, `safety.audit`, `tools.formatters`. Enforce with a ≤80-line budget on the executor module and a unit test that asserts zero business logic (mocks all collaborators and verifies call order only).

### Build sequence
- **CLI chat script survives past Milestone 4** as a permanent debugging tool. Keep it maintained alongside the UI. Great for isolating prompt issues from UI state issues.
- **Split Milestone 7** into **M7a** (write tools + rollback verified from CLI) and **M7b** (SSE dry-run/approval round-trip through the UI). M7a proves the risky core; M7b is pure state-management plumbing on top.

### Risks
- **R3 Pi 4 timeout** — base 10s generally, **15s for dashboard reloads** (many-card loads are slower). Configurable via add-on option, not hardcoded. All integration tests must run against a resource-constrained container (CPU quota + memory cap) to catch timing regressions before Pi users do.
- **R9 reconciler escalation** — after 3 consecutive failed sync cycles across different nights, mark the memory system as `reconciliation_degraded`, stop further auto-syncs, and surface a chat-injected message asking the user to clear the scratchpad manually or review recent extracts. Never silently lose extracts.

---

## 9. System Prompt Versioning (gap in original plan)

The system prompt (`data/system_prompt.txt`) and `data/security_rules.txt` are first-class behavioral artifacts. They'll evolve as we tune the agent, and a prompt change can look identical to a code bug in its effects.

**Requirements:**
- Each prompt file carries a semver-style header: `# version: 1.3.0` as the first line.
- `data/PROMPT_CHANGELOG.md` — append-only record of changes with date, version bump, and rationale (parallel to memory changelog).
- `context.assembler` stamps the prompt version onto every conversation turn written to `conversation.storage` (new column `prompt_version`).
- The Activity tab surfaces prompt version on each conversation so a user/debugger can correlate "Mylo started behaving weird on the 12th" with "prompt bumped from 1.2 → 1.3 on the 11th".
- Reconciler prompt (`memory.reconciler`) is also versioned the same way; its version stamps onto sync changelog entries.
- CI check: if either prompt file changes, the PR must also update `PROMPT_CHANGELOG.md` — block merge otherwise.

This costs almost nothing to build now and saves significant pain later when tuning behavior under real usage.

---

## 7. Testing Strategy

### Unit tests (`tests/unit/`)
Every module under `src/mylo/` has a mirrored test file. No IO — mocks for `ha.ws_client`, filesystem via `tmp_path`, LLM via a fake provider that replays canned tool-use sequences.

**Priorities:**
- `safety.sanitizer` — seed with adversarial payload corpus (injection phrases, unicode, very long strings).
- `memory.pruner` — deterministic ordering given fixed inputs.
- `validators.*` — valid/invalid YAML corpus per domain, pulled from real HA docs.
- `context.assembler` — snapshot tests (golden-prompt assertions) so changes to prompt assembly are visible.
- `tools.executor` — permission matrix: every (tier, confirmed?, rate-limited?) combination.
- `files.rollback` — simulate write/reload/verify with injected failure at each stage.

### Integration tests (`tests/integration/`)
Disposable HA container via docker-compose fixture. Seeded with a known config tarball before each test class.

**Minimal HA test environment:**
- HA Core in docker, exposed websocket.
- Integrations: `template`, `input_boolean`, `input_number`, `demo` (for varied entity types), `mqtt` (with a mosquitto container), `recorder` with sqlite.
- ~60 entities across 4 areas (`kitchen`, `living_room`, `office`, `garage`), mix of lights, sensors, switches, climate.
- 3 pre-existing automations (1 storage, 2 YAML).
- 2 dashboards (1 storage, 1 YAML).
- HACS not required in test; stub out custom-component validation.
- Fixture command: `scripts/dev_ha.sh` + `scripts/seed_ha.py`.

**Priority integration tests:**
- `ha.ws_client` reconnect after HA restart.
- Write-reload-verify loop: create automation, verify, then inject a broken one and assert auto-rollback.
- Dry-run vs real apply produces identical diff preview.
- Sanitizer doesn't break legitimate edge-case entity names (e.g. a light literally named "System Office").
- Sync job against a snapshotted context + seeded scratchpad produces expected diff.

### E2E tests (`tests/e2e/`)
Playwright against the built panel UI proxied through a test HA ingress.
- Happy path: "turn on kitchen lights" → tool call visible → response correct.
- Approval flow: tier-2 request → preview modal → approve → change applied.
- Panel close/reopen mid-stream resumes conversation.
- Memory tab edit persists and is reflected in next conversation.

### Manual verification checklist (per milestone)
A `MANUAL_VERIFICATION.md` added per milestone with reproducible user-facing steps. Each milestone's "Done" bullet corresponds to one checklist entry.

### CI
- `ci.yml`: ruff, mypy, pytest unit (every PR).
- Integration + e2e: on PR label `run-integration` and on main (they're slow — ~5 min).
- Release workflow: tag → multi-arch docker build → publish to ghcr.io → update repository manifest.

---

## Next Step

Review this plan. I expect pushback on the open questions especially — those shape module boundaries. Once resolved, I'll update this document, then start Milestone 0.
