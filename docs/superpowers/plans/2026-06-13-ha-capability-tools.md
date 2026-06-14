# HA Capability Tools — Implementation Plan

> Closes four common-HA-task gaps. Each item mirrors an existing tool pattern; all additive, behind the existing tier/approval model. Built item-by-item via subagent-driven development with spec + quality review and a commit per item. Integrations/backups deferred.

**Goal:** Let Mylo handle scenes, weekly-schedule + button helpers, automation/script trace debugging, and zones.

**Two patterns to mirror (already in the codebase):**
- **Live WS collection** (`manage_helpers`): WS `config/{type}/{create,update,delete}`, takes effect immediately, `tier=READ`, create/update/delete gated by returning `confirmation_required` when `not ctx.user_approved`. No `dry_run` param.
- **Package YAML write** (`modify_automation`): read-modify-write `/config/packages/agent.yaml`, `dry_run=True` default with a preview, validation, rollback.

**Every item:** Apache header + `from __future__ import annotations`; one module that calls `register(TOOL)`; add the module path to `registry._DEFAULT_MODULES`; add guidance to `src/mylo/data/system_prompt.txt`; unit tests in `tests/unit/` mirroring the analogous tool's tests (AsyncMock `ws_client`); `ruff check src tests` + full `pytest` green. Live behavior verified via HA rebuild.

---

## Item 1 — Schedule + button helpers (extend `manage_helpers`)

**Files:** `src/mylo/tools/write/manage_helpers.py`, `tests/unit/` (existing manage_helpers tests).

Add `input_button` and `schedule` to `HelperType` and the docstring. The WS command is already an f-string (`config/{helper_type}/{create|update|delete}`), so routing works once the type is allowed.

- `input_button` — payload is just `name` + `icon` (no value). `_build_payload` already adds name/icon; add an `elif ht == "input_button": pass` branch (no extra fields) so it's explicit.
- `schedule` — weekly time segments. Add a param:
  ```python
  schedule_blocks: dict[str, list[dict[str, str]]] | None = Field(
      default=None,
      description=(
          "For schedule: weekday -> list of {from, to} time segments, "
          "e.g. {'monday': [{'from': '08:00:00', 'to': '17:00:00'}]}. "
          "Weekday keys: monday..sunday."
      ),
  )
  ```
  In `_build_payload`, `elif ht == "schedule":` copy each present weekday key from `schedule_blocks` into the payload (HA expects `monday: [...], tuesday: [...]`, etc.).

**Tests:** create input_button (name only), create schedule (blocks → payload weekday keys), update/delete route to `config/schedule/*` and `config/input_button/*`, list works (registry filter by domain). Mirror existing manage_helpers test structure with AsyncMock `send_command`.

**Commit:** `feat(tools): manage_helpers supports schedule + input_button helpers`

---

## Item 2 — `modify_scene` (new write tool)

**Files:** create `src/mylo/tools/write/modify_scene.py`; register; tests `tests/unit/test_modify_scene.py`; prompt.

Mirror `modify_automation` exactly — read its full source and `modify_script.py` first and **reuse their package read-modify-write / dry-run / rollback machinery** (extract a shared helper module only if those helpers are private and duplication would be large; otherwise follow `modify_script`'s precedent).

- Actions: `create` | `update` | `delete` | `activate`.
- Persists a `scene:` list entry in `/config/packages/agent.yaml`. Scene YAML shape:
  ```yaml
  scene:
    - id: "<slug>"
      name: "<Name>"
      entities:
        light.kitchen: {state: "on", brightness: 200}
        switch.fan: "off"
  ```
- Params: `action`, `scene_id` (for update/delete/activate), `name`, `entities` (dict of entity_id → state-or-attrs), and a convenience `capture_entities: list[str] | None` — when set on `create`, read those entities' current state/attributes from `ctx.states`/registries and snapshot them into `entities`.
- `activate` does not edit YAML — it routes a `scene.turn_on` service call (reuse the call_service path or `ctx.ws_client` `call_service`), gated like other actions.
- Tier-2, `dry_run=True` default, preview shows the YAML diff (reuse modify_automation's preview).

**Tests:** create (explicit entities), create with `capture_entities` (snapshots current states from a mocked states source), update, delete, activate (asserts the service call), dry_run preview returns `preview: true` and writes nothing. Mirror `test` patterns used for modify_automation (check existing test file name first).

**Commit:** `feat(tools): modify_scene — create/update/delete/activate scenes`

---

## Item 3 — `query_traces` (new read tool)

**Files:** create `src/mylo/tools/read/query_traces.py`; register; tests `tests/unit/test_query_traces.py`; prompt.

Read-only (tier-1). Mirror `query_automations`' WS-read structure.

- Params: `item_type: Literal["automation", "script"]` (default "automation"), `item_id` (entity_id or object_id), `trace_id: str | None` (when omitted, return the list + the most recent trace).
- WS commands: `trace/list` with `{domain, item_id}` → recent runs (run_id, timestamp, state, script_execution); `trace/get` with `{domain, item_id, run_id}` → full trace (trigger, step-by-step `trace` dict, `error` if any, `last_step`).
- Output: a compact summary — for the latest (or requested) run: did it run, what triggered it, which steps executed, where it stopped, and any error — i.e. answer "why did/didn't this run."  Trim large blobs (reuse existing formatter/trim helpers if present).

**Tests:** trace/list returns runs; trace/get parsed into the summary (triggered + steps + error); no-traces case returns an empty/`no_traces` result; item_id normalization (accepts `automation.foo` or `foo`). Mock `ws_client.send_command` to return sample HA trace payloads.

**Commit:** `feat(tools): query_traces — inspect automation/script run traces`

---

## Item 4 — `modify_zones` (new write tool)

**Files:** create `src/mylo/tools/write/modify_zones.py`; register; tests `tests/unit/test_modify_zones.py`; prompt.

Mirror `modify_scene`/`modify_automation` (package YAML write). Persists a `zone:` list entry in `/config/packages/agent.yaml`. Zone YAML shape:
```yaml
zone:
  - name: "Work"
    latitude: 37.4
    longitude: -122.1
    radius: 100
    icon: "mdi:briefcase"
    passive: false
```

- Actions: `create` | `update` | `delete`.
- Params: `action`, `zone_id`/`name` (zones key on name/slug — match modify_automation's id handling), `latitude`, `longitude`, `radius` (meters, default 100), `icon`, `passive`.
- Do NOT allow editing the built-in `home` zone (it comes from core config) — return a clear error if targeted.
- Tier-2, `dry_run=True` default, preview shows the YAML diff.

**Tests:** create, update, delete, `home`-zone guard rejects, dry_run preview writes nothing. Mirror modify_scene tests.

**Commit:** `feat(tools): modify_zones — create/update/delete zones`

---

## Execution

Sequential (all four touch `registry.py` + `system_prompt.txt`). One implementer subagent per item → spec-compliance review → code-quality review → fix loop → commit. After all four: full `pytest` + `ruff` + `tsc` (no UI changes expected) and a final cross-item review. Then offer release (v1.3.0 — new capabilities).
