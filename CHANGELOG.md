# Changelog

All notable changes to Mylo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
