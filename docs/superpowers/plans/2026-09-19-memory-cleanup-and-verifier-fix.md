# Memory Cleanup and Verifier Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the nightly memory sync from failing, remove the dead learned-patterns subsystem, and make the automation background verifier look up the right entity.

**Architecture:** Three independent changes to existing modules. The reconciler gains a `stop_reason` check before parsing and a larger output cap. The `patterns` section is deleted from the memory data model and every producer/consumer (behavioral detector, pruner rules, reconciler compaction, selector, routes, UI). The automation verifier matches on `attributes.id` instead of a slug guess.

**Tech Stack:** Python 3.12, pydantic v2, ruamel.yaml, pytest + pytest-asyncio, React/TypeScript UI (Vite). Tests run with `pytest tests/unit`; lint with `ruff check src tests`, `ruff format src tests`, `mypy`.

**Spec:** `docs/superpowers/specs/2026-09-19-dashboard-plan-flow-design.md` §3 (sub-project 1).

## Global Constraints

- Every commit must leave `pytest tests/unit`, `ruff check src tests`, `ruff format --check src tests`, and `mypy` green. Run all four before each commit step.
- Do not run `npm run build` locally; the user tests the UI through a Home Assistant rebuild. TypeScript changes are checked with `cd ui && npx tsc --noEmit`.
- Do not hand-bump versions or tag. Releases go through `scripts/release.sh <version>` after a `CHANGELOG.md` entry exists.
- All source files carry the Apache 2.0 header block already present at the top of every file in `src/` and `tests/`. Copy it verbatim when creating a file.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Async tests in this repo need no decorator; `pytest-asyncio` is configured in auto mode.

---

### Task 1: Reconciler truncation detection

**Files:**
- Modify: `src/mylo/memory/reconciler.py:325-345`
- Test: `tests/unit/test_memory_sync.py:241-256` (the `_FakeResponse` / `_FakeProvider` helpers) and a new test after `test_reconciler_parses_yaml_and_preserves_user_sections`

**Interfaces:**
- Consumes: `ReconcileProvider.message(...)` returning an object with `.text` and `.stop_reason` (`src/mylo/llm/provider.py:57-70`).
- Produces: a new module constant `_RECONCILER_MAX_TOKENS = 20_000` in `reconciler.py`; a new log event name `memory.reconciler_truncated`.

- [ ] **Step 1: Give the fake provider a configurable stop reason**

In `tests/unit/test_memory_sync.py`, replace the `_FakeProvider` class (lines 249-256) with:

```python
class _FakeProvider:
    def __init__(self, reply: str, *, stop_reason: str = "end_turn") -> None:
        self.reply = reply
        self.stop_reason = stop_reason
        self.calls: list[dict[str, Any]] = []

    async def message(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(text=self.reply, stop_reason=self.stop_reason)
```

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_memory_sync.py` directly after `test_reconciler_parses_yaml_and_preserves_user_sections`:

```python
async def test_reconciler_detects_truncated_output(tmp_path: Path) -> None:
    """A max_tokens stop means the YAML is cut mid-document. Don't parse
    it — report truncation and leave memory untouched."""
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()
    (tmp_path / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, '
        'content: "likes warm light", recorded: "2026-04-12", '
        'confidence: 0.9, conversation_id: "c1"}\n'
    )
    # Half a document: valid up to the cut, then nothing.
    provider = _FakeProvider(
        reply="version: 2\nnotes:\n  - id: n1\n    content: 'cut off he",
        stop_reason="max_tokens",
    )

    result = await run_sync(
        store=store,
        provider=provider,
        registries=None,
        model="claude-haiku-4-5-20251001",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    assert result.updated is None
    assert "truncated" in result.summary
    assert "malformed" not in result.summary
    assert len(provider.calls) == 1
    assert provider.calls[0]["max_tokens"] == 20_000
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/unit/test_memory_sync.py::test_reconciler_detects_truncated_output -v`
Expected: FAIL — summary says "malformed YAML" and `max_tokens` is 8192.

- [ ] **Step 4: Implement truncation detection**

In `src/mylo/memory/reconciler.py`, add a constant next to `_SCRATCHPAD_RECONCILE_LIMIT` (after line 91):

```python
# Output cap for the merge. The reconciler re-emits the whole memory
# file, so this must comfortably exceed the file's size; Haiku 4.5
# supports 64k output tokens. Truncation is detected via stop_reason
# below rather than left to surface as a YAML parse error.
_RECONCILER_MAX_TOKENS = 20_000
```

Replace the provider call and the parse block (lines 325-345) with:

```python
    response = await provider.message(
        system=prompt,
        messages=[{"role": "user", "content": user_msg}],
        tools=[],
        model=model,
        max_tokens=_RECONCILER_MAX_TOKENS,
    )

    raw_text = getattr(response, "text", "") or ""
    if getattr(response, "stop_reason", "") == "max_tokens":
        # The document was cut mid-stream. Parsing it would fail with a
        # misleading "malformed YAML" — say what actually happened.
        log.error(
            "memory.reconciler_truncated",
            chars=len(raw_text),
            max_tokens=_RECONCILER_MAX_TOKENS,
        )
        return ReconcileResult(
            updated=None,
            summary=(
                f"reconciler output truncated at {_RECONCILER_MAX_TOKENS} "
                "max_tokens; memory untouched, scratchpad preserved"
            ),
            conflicts_added=0,
            prune_report=prune_report,
            raw_output=raw_text,
        )
    try:
        proposed = _parse_reconciler_output(raw_text)
    except Exception as exc:
        log.exception("memory.reconciler_parse_failed", error=str(exc))
        return ReconcileResult(
            updated=None,
            summary=f"reconciler returned malformed YAML ({exc}); scratchpad preserved",
            conflicts_added=0,
            prune_report=prune_report,
            raw_output=raw_text,
        )
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/test_memory_sync.py -v`
Expected: all PASS, including the new test.

- [ ] **Step 6: Lint and commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/memory/reconciler.py tests/unit/test_memory_sync.py
git commit -m "fix(memory): detect truncated reconciler output, raise cap to 32k

The nightly merge asked the model to re-emit the whole memory file with
an 8192-token output cap. Files longer than that were cut mid-scalar and
reported as malformed YAML. Check stop_reason before parsing and raise
the cap.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Automation verifier matches on `attributes.id`

**Files:**
- Modify: `src/mylo/files/rollback.py:610-645` (add a verifier next to `automation_loaded_verifier`)
- Modify: `src/mylo/tools/write/modify_automation.py:49-52` and `:200-204`
- Test: `tests/unit/test_rollback.py` (append after `test_automation_loaded_verifier_fails_when_missing`)

**Interfaces:**
- Consumes: `Verifier = Callable[[HaWsClient], Awaitable[tuple[bool, str, dict[str, Any]]]]` (`rollback.py:95`), `CommandError` (`ws_client.py:66`).
- Produces: `automation_by_config_id_verifier(automation_id: str) -> Verifier` in `mylo.files.rollback`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_rollback.py`:

```python
async def test_automation_by_config_id_verifier_matches_on_attribute(tmp_path: Path) -> None:
    """HA names the entity after the alias slug, not the config id. The
    verifier must find the entity by attributes.id regardless of slug."""
    from mylo.files.rollback import automation_by_config_id_verifier

    states = [
        {
            "entity_id": "automation.berkley_room_light_stoplight_schedule",
            "state": "on",
            "attributes": {"id": "mylo_berkley_room_light_stoplight_schedule"},
        }
    ]
    client = _FakeClient(states_after=states)
    verify = automation_by_config_id_verifier("mylo_berkley_room_light_stoplight_schedule")
    ok, message, details = await verify(client)  # type: ignore[arg-type]
    assert ok
    assert details["entity_id"] == "automation.berkley_room_light_stoplight_schedule"
    assert details["state"] == "on"
    assert "loaded" in message


async def test_automation_by_config_id_verifier_fails_when_absent(tmp_path: Path) -> None:
    from mylo.files.rollback import automation_by_config_id_verifier

    states = [
        {"entity_id": "automation.other", "state": "on", "attributes": {"id": "other_id"}},
        {"entity_id": "light.kitchen", "state": "on", "attributes": {"id": "mylo_x"}},
    ]
    client = _FakeClient(states_after=states)
    verify = automation_by_config_id_verifier("mylo_x")
    ok, message, _ = await verify(client)  # type: ignore[arg-type]
    assert not ok
    assert "mylo_x" in message


async def test_automation_by_config_id_verifier_rejects_unavailable(tmp_path: Path) -> None:
    from mylo.files.rollback import automation_by_config_id_verifier

    states = [
        {"entity_id": "automation.a", "state": "unavailable", "attributes": {"id": "mylo_a"}},
    ]
    client = _FakeClient(states_after=states)
    verify = automation_by_config_id_verifier("mylo_a")
    ok, message, details = await verify(client)  # type: ignore[arg-type]
    assert not ok
    assert "unavailable" in message
    assert details["entity_id"] == "automation.a"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_rollback.py -k config_id -v`
Expected: FAIL with `ImportError: cannot import name 'automation_by_config_id_verifier'`.

- [ ] **Step 3: Add the verifier**

In `src/mylo/files/rollback.py`, insert directly after `automation_loaded_verifier` (before `def entity_exists_verifier`):

```python
def automation_by_config_id_verifier(automation_id: str) -> Verifier:
    """Verify the automation with config ``id == automation_id`` loaded.

    HA derives an automation's ``entity_id`` from its alias, not its
    ``id`` — so a slug built from the id (``automation.mylo_<slug>``)
    never matches. The config id is exposed on the entity as
    ``attributes.id``; match on that instead.
    """

    async def _verify(client: HaWsClient) -> tuple[bool, str, dict[str, Any]]:
        try:
            states = await client.send_command("get_states")
        except CommandError as exc:
            return False, f"{exc.code}: {exc.message}", {}

        if not isinstance(states, list):
            return False, "could not fetch states", {}

        match = next(
            (
                s
                for s in states
                if isinstance(s, dict)
                and str(s.get("entity_id", "")).startswith("automation.")
                and (s.get("attributes") or {}).get("id") == automation_id
            ),
            None,
        )
        if match is None:
            return False, f"no automation with id {automation_id!r} present after reload", {}

        entity_id = str(match.get("entity_id"))
        current = match.get("state")
        details = {"entity_id": entity_id, "state": current}
        if current in ("on", "off"):
            return True, f"{entity_id} loaded (state={current})", details
        return False, f"{entity_id} present but state is {current!r}", details

    return _verify
```

- [ ] **Step 4: Use it in modify_automation**

In `src/mylo/tools/write/modify_automation.py`, change the import block (lines 49-52) to:

```python
from mylo.files.rollback import (
    apply_optimistic_reload_all,
    automation_by_config_id_verifier,
)
```

Replace the verifier construction (lines 200-204) with:

```python
    verify = (
        automation_by_config_id_verifier(automation_id)
        if params.action in ("create", "update", "enable")
        else None
    )
```

If `_slug` is now unused anywhere except `_derive_id`, leave it; `_derive_id` still calls it.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/test_rollback.py tests/unit -q -k "rollback or automation"`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/files/rollback.py src/mylo/tools/write/modify_automation.py tests/unit/test_rollback.py
git commit -m "fix(automation): verify by attributes.id, not a slug of the config id

HA names automation entities after the alias slug. The background verify
looked for automation.mylo_<slug> and could never match, so every write
logged 'not present after reload'.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Remove behavioral pattern detection

**Files:**
- Delete: `src/mylo/monitor/behavioral.py`
- Modify: `src/mylo/monitor/scheduler.py:231-263` (nightly step 3)
- Modify: `src/mylo/memory/pruner.py:386-418` (`enforce_pattern_cap`)
- Test: `tests/unit/test_memory_sync.py` (remove `test_behavioral_pattern_id_stable_under_minute_drift`, `test_enforce_pattern_cap_keeps_strongest`, `test_enforce_pattern_cap_protects_non_behavioral`, `test_enforce_pattern_cap_noop_under_cap`, `test_detect_patterns_reports_refreshes`, and the `_write_transitions` helper if nothing else uses it)

**Interfaces:**
- Consumes: nothing new.
- Produces: `scheduler._nightly_job` no longer touches `memory.patterns`. `mylo.memory.pruner.enforce_pattern_cap` no longer exists.

- [ ] **Step 1: Remove the tests that exercise the detector**

In `tests/unit/test_memory_sync.py` delete these test functions and the comment block above `test_enforce_pattern_cap_keeps_strongest` (the one that starts "transition window and re-added them"):

- `test_behavioral_pattern_id_stable_under_minute_drift`
- `test_enforce_pattern_cap_keeps_strongest`
- `test_enforce_pattern_cap_protects_non_behavioral`
- `test_enforce_pattern_cap_noop_under_cap`
- `test_detect_patterns_reports_refreshes`

Then check whether `_write_transitions` is still referenced:

Run: `grep -n "_write_transitions" tests/unit/test_memory_sync.py`
If the only remaining hit is the `def` line, delete the helper too. Also remove any now-unused imports (`json`, `timedelta`) that ruff flags.

- [ ] **Step 2: Run the file to confirm the remaining tests still pass**

Run: `pytest tests/unit/test_memory_sync.py -q`
Expected: PASS (the deleted tests are simply gone).

- [ ] **Step 3: Delete the detector and its scheduler step**

```bash
git rm src/mylo/monitor/behavioral.py
```

In `src/mylo/monitor/scheduler.py`, delete the whole block from the comment `# 3. Behavioral pattern detection.` through `log.exception("nightly.behavioral_failed")` (lines 231-263). Renumber the following comment `# 4. Fold transitions into learned profiles.` to `# 3.`.

In `src/mylo/memory/pruner.py`, delete `enforce_pattern_cap` (lines 386-418, from `def enforce_pattern_cap` through `return dropped`).

- [ ] **Step 4: Confirm nothing imports the removed symbols**

Run: `grep -rn "behavioral\|enforce_pattern_cap" src tests`
Expected: hits only in `src/mylo/monitor/transitions.py` docstrings (the words "behavioral pattern" in prose) and `src/mylo/server/app.py:215` (a comment). Edit `app.py:215` to read `# and records on/off transitions for the learned-profile engine.`

- [ ] **Step 5: Run the suite, lint, commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add -A src/mylo/monitor src/mylo/memory/pruner.py src/mylo/server/app.py tests/unit/test_memory_sync.py
git commit -m "refactor(monitor): remove behavioral pattern detection

Nightly pattern detection wrote memory.patterns, which nothing read: not
the prompt, not the findings detectors. It was the bulk of the reconciler
payload and the source of the id-drift and cap oscillation bugs.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Remove pattern pruner rules

**Files:**
- Modify: `src/mylo/memory/pruner.py:20-35` (docstring), `:59-73` (constants), `:148-153` (plan_prune rule 5), `:172-200` (apply_prune), `:284-384` (three rule functions)
- Test: `tests/unit/test_memory_sync.py` (remove `test_pruner_drops_low_confidence_old_patterns`, `test_pruner_drops_stale_behavioral_patterns_regardless_of_confidence`, `test_pruner_caps_pattern_count_keeping_strongest`)

**Interfaces:**
- Produces: `plan_prune` and `apply_prune` keep their signatures. `PruneCandidate.section` comment no longer lists `"patterns"`.

- [ ] **Step 1: Remove the three pattern pruner tests**

Delete from `tests/unit/test_memory_sync.py`:
- `test_pruner_drops_low_confidence_old_patterns`
- `test_pruner_drops_stale_behavioral_patterns_regardless_of_confidence`
- `test_pruner_caps_pattern_count_keeping_strongest`

- [ ] **Step 2: Write a regression test that pruning ignores unknown sections**

Append to `tests/unit/test_memory_sync.py` after `test_pruner_drops_old_rejections`:

```python
def test_pruner_has_no_pattern_rules() -> None:
    """Patterns were removed from the data model; the pruner must not
    reference them (no attribute access, no candidate section)."""
    mem = empty_memory()
    report = plan_prune(mem, now=NOW)
    assert all(c.section != "patterns" for c in report.candidates)
    pruned = apply_prune(mem, report)
    assert "patterns" not in pruned.model_dump()
```

This will fail at the last assertion until Task 6 removes the field; that is expected. Mark it `@pytest.mark.xfail(strict=True, reason="patterns field removed in Task 6")` for now and remove the marker in Task 6.

- [ ] **Step 3: Delete the rules**

In `src/mylo/memory/pruner.py`:

1. Docstring lines 27-29: replace the rule-5 text with `5. (removed — patterns no longer exist)` and renumber `6.` to `5.`... Simpler: replace the numbered list with:

```
1. Expired TTL items
2. Observations never confirmed, older than 90 days
3. Lowest reference_count + oldest last_referenced
4. Resolved known_issues (archive, not delete)
5. Rejected suggestions older than 6 months
```

2. Delete the constants `PATTERN_MAX_AGE`, `PATTERN_STALE_MAX_AGE`, `MAX_PATTERNS` and their comment blocks (lines 60-73). Keep `OBSERVATION_MAX_AGE`, `REJECTED_MAX_AGE`, `LOW_CONFIDENCE_THRESHOLD` only if still referenced (run `grep -n LOW_CONFIDENCE_THRESHOLD src/mylo/memory/pruner.py`; if only the definition remains, delete it).

3. In `PruneReason` (starts line 75), remove the literals `"low_confidence_pattern"`, `"stale_pattern"`, `"pattern_cap_exceeded"`.

4. Line 91 comment: `section: str  # "notes" | "known_issues" | "rejected"`.

5. In `plan_prune`, delete lines 148-153 (the rule-5 block from the comment `# 5. Low-confidence old patterns` through `candidates.extend(_excess_patterns(...))`). Renumber the `# 6.` comment to `# 5.`.

6. In `apply_prune`, remove `"patterns": set(),` from `to_drop` and delete the three lines that rebuild `data["patterns"]`.

7. Delete the functions `_low_confidence_patterns`, `_stale_patterns`, `_excess_patterns` entirely.

- [ ] **Step 4: Run, lint, commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/memory/pruner.py tests/unit/test_memory_sync.py
git commit -m "refactor(memory): drop pattern pruner rules

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Remove pattern compaction from the reconciler

**Files:**
- Modify: `src/mylo/memory/reconciler.py:170-225` (`compact_payload_sections`, `_reattach_compacted`), `:290-293` (comment)
- Test: `tests/unit/test_reconciler_compaction.py`

**Interfaces:**
- Produces: `compact_payload_sections(memory, *, budget_tokens) -> tuple[MemoryFile, str, dict[str, list[Any]]]` where the dropped dict has exactly one key, `"notes"`. `_reattach_compacted(merged, dropped)` reads only `dropped["notes"]`.

- [ ] **Step 1: Update the compaction tests**

In `tests/unit/test_reconciler_compaction.py`:

- In `test_fast_path_when_already_small`, change `assert dropped == {"notes": [], "patterns": []}` to `assert dropped == {"notes": []}`.
- In `test_reattach_restores_dropped_without_duplicating`, remove the `"patterns": [],` line from the `dropped` dict.

- [ ] **Step 2: Run them to see the fast-path test fail**

Run: `pytest tests/unit/test_reconciler_compaction.py -v`
Expected: `test_fast_path_when_already_small` FAILS (dict still has a `patterns` key).

- [ ] **Step 3: Simplify the reconciler**

Replace `compact_payload_sections` and `_reattach_compacted` in `src/mylo/memory/reconciler.py` with:

```python
def compact_payload_sections(
    memory: MemoryFile, *, budget_tokens: int
) -> tuple[MemoryFile, str, dict[str, list[Any]]]:
    """Return a copy of ``memory`` whose serialized size fits ``budget_tokens``,
    dropping the most expendable plain notes first. Critical items
    (user_confirmed / critical-priority notes) are never dropped.

    Returns ``(compacted_memory, marker, dropped)`` where ``dropped`` maps
    each section to the items removed — the caller re-attaches these after
    the merge so compaction never loses data. Drops in chunks to keep this
    O(rounds times n), not O(n squared).
    """
    work = copy.deepcopy(memory)
    dropped: dict[str, list[Any]] = {"notes": []}
    if estimate_tokens(work.model_dump_json()) <= budget_tokens:
        return work, "", dropped

    protected = [n for n in work.notes if _note_protected(n)]
    droppable = [n for n in work.notes if not _note_protected(n)]
    while droppable and estimate_tokens(work.model_dump_json()) > budget_tokens:
        chunk = max(1, len(droppable) // 10)
        removed = droppable[-chunk:]
        del droppable[-chunk:]
        dropped["notes"].extend(removed)
        work.notes = protected + droppable
    work.notes = protected + droppable

    marker = f"+{len(dropped['notes'])} notes compacted" if dropped["notes"] else ""
    return work, marker, dropped


def _reattach_compacted(merged: MemoryFile, dropped: dict[str, list[Any]]) -> None:
    """Re-add notes dropped from the payload only to fit context.

    They skipped this pass's reconciliation, so add back any whose id isn't
    already present in the merged result (the LLM never saw them and so
    can't have changed them).
    """
    existing_note_ids = {n.id for n in merged.notes}
    for note in dropped.get("notes", []):
        if note.id not in existing_note_ids:
            merged.notes.append(note)
```

Remove the `Counter` import if it is now unused (ruff will flag it). Update the comment at lines 290-293 to read `# expendable notes from what the LLM sees`.

- [ ] **Step 4: Run, lint, commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/memory/reconciler.py tests/unit/test_reconciler_compaction.py
git commit -m "refactor(memory): compaction handles notes only

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Remove the `patterns` field, selector trigger, and routes

**Files:**
- Modify: `src/mylo/memory/schema.py:195-204` (delete `Pattern`), `:357` (delete field)
- Modify: `src/mylo/memory/store.py:75-100` (`load`)
- Modify: `src/mylo/context/selector.py:44`
- Modify: `src/mylo/server/routes_memory.py:72`, `:241`
- Test: `tests/unit/test_context_assembler.py:173-175`, `tests/unit/test_memory.py` (new test), `tests/unit/test_memory_sync.py` (remove xfail marker from Task 4)

**Interfaces:**
- Produces: `MemoryFile` without `patterns`; `MemoryStore.load()` strips a legacy `patterns` key.

- [ ] **Step 1: Write the failing store test**

Append to `tests/unit/test_memory.py`:

```python
async def test_store_load_drops_legacy_patterns_key(tmp_path: Path) -> None:
    """context.yaml files written before the patterns removal carry a
    populated patterns: list. It must load cleanly and disappear on save."""
    (tmp_path / "context.yaml").write_text(
        "version: 2\n"
        "patterns:\n"
        "  - id: p1\n"
        "    description: old\n"
        "    confidence: 0.9\n"
        "notes: []\n"
    )
    store = MemoryStore(mylo_data_dir=tmp_path)
    memory = await store.load()
    assert not hasattr(memory, "patterns") or "patterns" not in memory.model_dump()
    await store.save(memory, note="resave")
    assert "patterns" not in (tmp_path / "context.yaml").read_text()
```

Check the top of `tests/unit/test_memory.py` for an existing `MemoryStore` import and `Path` import; add them if missing.

- [ ] **Step 2: Replace the selector test**

In `tests/unit/test_context_assembler.py`, replace `test_selector_triggers_patterns_on_usually_keyword` with:

```python
def test_selector_does_not_emit_removed_patterns_section() -> None:
    sections = select_sections("we usually turn off lights at 11pm", empty_memory())
    assert "patterns" not in sections
```

- [ ] **Step 3: Run both to confirm they fail**

Run: `pytest tests/unit/test_memory.py::test_store_load_drops_legacy_patterns_key tests/unit/test_context_assembler.py::test_selector_does_not_emit_removed_patterns_section -v`
Expected: both FAIL.

- [ ] **Step 4: Remove the model and field**

In `src/mylo/memory/schema.py`:
- Delete the `Pattern` class (lines 195-204).
- Delete `patterns: list[Pattern] = Field(default_factory=list)` from `MemoryFile`.

In `src/mylo/memory/store.py`, inside `load()`, after `parsed = load_yaml(raw) or {}` and its except block, add:

```python
            if isinstance(parsed, dict) and "patterns" in parsed:
                # Legacy section (removed 2026-09). MemoryFile allows extra
                # keys, so it would survive round-trips forever; strip it.
                parsed.pop("patterns", None)
                log.info("memory.legacy_patterns_dropped")
```

- [ ] **Step 5: Remove the selector trigger and route references**

In `src/mylo/context/selector.py`, delete the line `"patterns": ("usually", "normally", "pattern", "schedule", "typical", "every"),`.

In `src/mylo/server/routes_memory.py`:
- Delete `"patterns": len(memory.patterns),` at line 72.
- Change `_DELETABLE_SECTIONS = {"notes", "known_issues", "patterns", "rejected"}` to `{"notes", "known_issues", "rejected"}`.

In `tests/unit/test_memory_sync.py`, remove the `@pytest.mark.xfail(...)` marker from `test_pruner_has_no_pattern_rules`.

- [ ] **Step 6: Find any remaining references**

Run: `grep -rn "\.patterns\b\|\bPattern\b\|\"patterns\"" src tests | grep -v "re.Pattern\|_INJECTION_PATTERNS"`
Expected: no hits. Fix any that appear (e.g. a test fixture constructing `Pattern(...)`).

- [ ] **Step 7: Run, lint, commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/memory/schema.py src/mylo/memory/store.py src/mylo/context/selector.py src/mylo/server/routes_memory.py tests/unit/test_memory.py tests/unit/test_context_assembler.py tests/unit/test_memory_sync.py
git commit -m "refactor(memory)!: remove the patterns section from context.yaml

Legacy files still load; the key is stripped on load and gone on the
next save.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Remove patterns from the UI

**Files:**
- Modify: `ui/src/components/MemoryTab.tsx:30`, `:285-297`, `:549-570`, `:796-797`
- Modify: `ui/src/types.ts:89-96`, `:133`

**Interfaces:**
- Produces: `MemoryFull` without `patterns`; no `MemoryPattern` type.

- [ ] **Step 1: Edit the types**

In `ui/src/types.ts`, delete the `MemoryPattern` interface (lines 89-96) and the line `patterns: MemoryPattern[];` inside `MemoryFull`.

- [ ] **Step 2: Edit the tab**

In `ui/src/components/MemoryTab.tsx`:
- Remove `MemoryPattern,` from the type import block.
- Delete the `<Section title={`Patterns (...)`}>...</Section>` block (lines 285-297).
- Delete the `PatternRow` function (lines 549-570).
- In the footer summary (lines 796-797), delete the two spans that render `memory.patterns.length` and the ` patterns · ` label.

- [ ] **Step 3: Type-check**

Run: `cd ui && npx tsc --noEmit`
Expected: no errors. If `tsc` reports an unused `handleDelete` section-type union that still lists `"patterns"`, remove that literal too.

- [ ] **Step 4: Commit**

```bash
git add ui/src/components/MemoryTab.tsx ui/src/types.ts
git commit -m "feat(ui): drop the patterns section from the Memory tab

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Changelog entry and release

**Files:**
- Modify: `CHANGELOG.md` (insert above `## [1.5.0b5]`)

- [ ] **Step 1: Write the changelog entry**

Insert directly above `## [1.5.0b5] — 2026-08-12`:

```markdown
## [1.5.0b6] — 2026-09-19

> ⚠️ **BETA — test at your own risk.** Continues the 1.5.0 beta. Back up your `context.yaml` before updating.

### Changed
- **Learned behavioral patterns are gone.** The nightly pattern detector wrote hundreds of "light.x usually turns off at 22:30" entries into memory that nothing read — not the chat prompt, not the monitoring detectors. They were the bulk of the memory file and the cause of a run of sync bugs. The Memory tab no longer shows a Patterns section. Existing files load fine; the old section is dropped on the next save. Findings still come from the learned per-entity profiles, which are unchanged.

### Fixed
- **Nightly memory sync no longer fails every night.** The merge asked the model to re-emit the whole memory file but capped the reply at 8k tokens, so any file longer than that was cut mid-line and reported as "malformed YAML". The cap is now 32k and a truncated reply is detected and reported as such instead of being parsed.
- **Automation writes no longer log a false "not present after reload".** The background check looked for an entity named after the automation's id; Home Assistant names it after the alias. The check now matches on the automation's id attribute.
```

- [ ] **Step 2: Commit the changelog**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): 1.5.0b6 — memory cleanup + verifier fix

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 3: Cut the release (only when the user says to release)**

Run: `DRY_RUN=1 scripts/release.sh 1.5.0b6` first and read its output. Then, with the user's go-ahead, `scripts/release.sh 1.5.0b6`.
