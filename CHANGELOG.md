# Changelog

All notable changes to Mylo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
