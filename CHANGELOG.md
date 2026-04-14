# Changelog

All notable changes to Mylo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
