# Trust — Design

**Status:** approved as a section-level design in conversation 2026-09-22;
second of three UX releases (1.7 Live turn → **1.8 Trust** → 1.9 Surface).
**Target release:** 1.8.0.

## 1. Problem

Mylo's safety plumbing is sound, but the user is not told the truth at the
moments that build or break trust:

1. **"Done" before verified.** On a large home every automation, script,
   scene, zone and rename write goes through `apply_optimistic_reload_all`
   (`files/rollback.py`), which returns `RollbackResult(ok=True)` before
   HA has reloaded. The model reads `ok: true` and says "done". If the
   background verify later fails, the only signal is an HA persistent
   notification. A rollback is recorded as a flag in the Activity tab.
2. **Silent nightly failures.** `_nightly_job` (`monitor/scheduler.py`)
   logs a reconcile failure and returns. `last_sync` is not updated, the
   scratchpad is not drained, and nothing surfaces to the user.
3. **Hidden trust artifacts.** Backups are taken on every write but the
   prompt only asks the model to mention them for dashboard failures.
   Session cost is priced against a hardcoded Sonnet rate
   (`ui/src/store.ts` `model: "claude-sonnet-4-6"`, `setModel` never called).
4. **Approval is a turn-wide boolean.** `Permissions.check` allows any
   tier-2/3 call once `user_approved` is true. Only dashboards and cards
   bind approval to a specific id. Multi-change approvals are
   all-or-nothing, and a delete gets the same green Apply as a light.

## 2. Goals

1. A write says "applied, verifying" until verification finishes, and the
   outcome (verified, failed, rolled back) lands in the chat.
2. A failed nightly sync is visible in the panel with the last good sync.
3. The model states the backup path after every write; cost is priced
   against the configured model.
4. Approval is bound to exactly the calls previewed; the user can approve a
   subset; destructive changes look different.

## 3. Non-goals

- Changing what tools do or their parameters (except `manage_*` tiering,
  §4.4).
- Synchronous verification on large homes (the optimistic path stays).
- Labels, layout, mobile, accessibility (1.9).
- A new notification channel; the HA persistent notification stays as the
  closed-panel fallback.

## 4. Design

### 4.1 Verification outcomes

**Store.** New module `src/mylo/files/verifications.py`:

```python
@dataclass(slots=True)
class Verification:
    id: str                 # "vf_" + 12 hex
    tool: str               # "modify_automation", "rename_entities", …
    target: str             # file name or entity id shown to the user
    status: str             # "pending" | "verified" | "failed" | "rolled_back"
    message: str
    conversation_id: str
    requested_at: str       # ISO UTC
    completed_at: str | None = None
    acknowledged: bool = False

class VerificationLog:
    def __init__(self, path: Path, *, capacity: int = 50, clock: Callable[[], datetime] | None = None) -> None
    def start(self, *, tool: str, target: str, conversation_id: str) -> Verification
    def complete(self, id: str, *, status: str, message: str) -> Verification | None
    def acknowledge(self, id: str) -> bool
    def recent(self, *, hours: float = 24.0) -> list[Verification]   # newest first
    def unacknowledged(self) -> list[Verification]                    # completed only
```

Persisted as JSON at `<mylo_data_dir>/verifications.json` on every
mutation (atomic write); loaded at construction; capped at `capacity`
newest. Exposed as `AppKeys.VERIFICATIONS`; `ToolContext` gains
`verifications: VerificationLog | None = None` and the chat route passes
it through like `plans`/`cards`.

**Producers.** `apply_optimistic_reload_all` gains
`verifications: VerificationLog | None = None` and `target: str = ""`.
When given, it calls `start()` before the write and passes the id to
`_background_verify_reload_all`, which calls `complete()` with
`verified` (verify ok), `failed` (reload_all error, reconnect timeout,
verifier false or raised). The same for `rename_entities`' background
verify (`_record_rename_*`), with `target=new_id`. Any future rollback path
completes with `rolled_back`. The audit entries and the persistent
notification are unchanged.

**Tool results.** `RollbackResult` gains `verification: str = "synchronous"`
and `verification_id: str | None = None`; `to_dict()` includes both. The
optimistic path sets `verification="pending"`. Every tool that uses the
optimistic path copies `verification` and `verification_id` to the
top level of its result envelope next to `apply`, so the model sees
`"verification": "pending"` without digging.

**Prompt (0.8.0).** Add to the write-flow rules:

> When a write result has `verification: "pending"`, say the change was
> applied and that Mylo is verifying it in the background; never say it is
> done or working. When a result reports `backup_path`, tell the user
> where the backup is in one short sentence. If the system prompt lists a
> verification outcome the user has not seen, mention it before anything
> else.

**Model context.** `assemble_system_prompt` gains
`verifications: list[Verification] | None = None`; when non-empty it adds
`TextSurface("verifications", …)` right after `critical_memory`, listing
each outcome from the last 24 h as one line
(`- 14:02 modify_automation "Porch light at dusk" — verified` /
`— FAILED: websocket never reconnected`). The route passes
`verifications.recent()`.

**Panel.** `/api/status` gains `verifications: Verification[]` (the
unacknowledged completed ones). The Header already polls status every
30 s; it gains an `onStatus(status)` prop. `App` keeps
`verifications` state; when not `sending`, each unacknowledged outcome
renders as a `VerificationCard` below the messages: a one-line status
("Verified · Porch light at dusk" in accent, "Verification failed ·
sensors.yaml" in error, "Rolled back · …" in warning), the message, the
relative time, and a ✕ that calls `POST /api/verifications/{id}/ack`.
Cards render newest last, above the approval/question cards. Pending
verifications are not shown as cards; the model's "applied, verifying"
sentence covers that window.

### 4.2 Nightly sync visibility

`MemoryFile` gains `last_sync_error: str | None = None` and
`last_sync_attempt: str | None = None`. In `_nightly_job`:

- On success (`result.updated is not None`): `last_sync_error = None`,
  and a `sync_failed` finding, if present, is removed
  (`findings.remove_finding(mem, "mylo_sync_failed")`, a new one-liner).
- On any non-success (`updated is None` and the summary is not the
  "nothing to do" case, or the `except` branch): `last_sync_attempt = now`,
  `last_sync_error = <summary or exception class>`, and
  `record_sync_failure(mem, summary, now)` upserts finding
  `id="mylo_sync_failed"`, `type="sync_failed"`, `entity_id=""`,
  `title="Memory sync failed"`, `message=f"{summary}. Your notes are kept and Mylo will retry tonight."`,
  `confidence=1.0`. Save the memory either way.

The "nothing to do" summary ("no new scratchpad entries or state changes
since last sync") counts as success for `last_sync_error` but does not
touch `last_sync`.

`/api/status.memory` gains `last_sync_error` and `last_sync_attempt`. The
Header's "synced 3h ago" line becomes "sync failed 3h ago" in the error
colour when `last_sync_error` is set; the Memory tab's last-sync line
shows the error text beneath it. The finding reaches the catch-up banner
and Findings panel through the existing `pending_actions` path.

### 4.3 Backups and cost

- Backup rule: the prompt sentence in §4.1.
- `/api/status` gains `model: str` and `provider: str` (from `AppConfig`).
  `App` calls `useSession.setModel(status.model)` on the first status
  fetch. `ui/src/lib/cost.ts::estimateCost(usage, model, provider?)`: an
  unknown model on provider `"ollama"` prices at zero; unknown otherwise
  keeps the Sonnet fallback. `MODEL_CONTEXT_WINDOW` becomes
  `contextWindowFor(model)` with a small table (Claude 200k, gpt-4o 128k,
  gemini 1M, default 128k) used by the Composer's budget line.

### 4.4 Scoped approval

**Fingerprint.** `src/mylo/safety/approval.py`:

```python
def preview_id(tool_name: str, params: Mapping[str, Any]) -> str:
    """'pv_' + sha256(tool_name + canonical JSON of params without dry_run)[:12]."""
```

Canonical JSON: `json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)`
after removing the `dry_run` key at the top level only.

**ToolDefinition.** Two new optional fields:

- `approval_key: str | None = None` — for tools that bind approval to an
  id they minted (`apply_dashboard_plan` → `"plan_id"`,
  `apply_custom_card` → `"card_id"`). The executor uses
  `params[approval_key]` as the id instead of the fingerprint.
- `free_actions: frozenset[str] = frozenset()` — parameter `action` values
  that need no approval (`"list"`). A call whose `action` is in the set is
  treated as READ for permission purposes (not cached).

**Executor.** In `execute`, after validation:

1. Effective tier: `tool.tier`, downgraded to `READ` when
   `raw_params.get("action") in tool.free_actions`.
2. Compute `pid = params[approval_key]` if set, else
   `preview_id(tool.name, raw_params)`.
3. Tier-2 dry-run (allowed as today): after the handler returns OK with a
   dict `data`, set `data["preview_id"] = pid`.
4. Tier-2/3 non-dry-run: `Permissions.check` as today for the boolean;
   then, if allowed, require `pid in ctx.approved_plan_ids`; otherwise
   return `ToolResult.error("not_approved", "this exact change was not in the set the user approved — preview it again", data={"preview_id": pid})`.
5. `confirmation_required` errors (tier-2 without dry_run and no approval,
   tier-3 without approval) carry `data={"preview_id": pid, "tool": name, "params": raw_params}`
   so the panel can list them for approval.

`ctx.approved_plan_ids` keeps its name and wire field
(`approved_plan_ids`); it now holds plan ids, card ids and preview ids.
`apply_dashboard_plan`/`apply_custom_card` keep their own checks
(defense in depth).

**manage_\* tools.** `manage_helpers`, `manage_labels`, `manage_monitored`,
`manage_notification_filters` become `tier=Tier.MODIFY` with
`free_actions=frozenset({"list"})`, and their hand-rolled
`ctx.user_approved` checks are deleted. Their first mutating call in a turn
returns `confirmation_required` with the fingerprint; the panel shows it;
Apply authorises exactly that call.

**Prompt (0.8.0).** Amend the write flow:

> On the approval turn, re-issue every previewed change with exactly the
> same parameters. If a call returns `not_approved`, the user chose not to
> apply that one: skip it, do not retry it, and say which changes you
> skipped.

And the tier-3 rule gains: "`manage_helpers`, `manage_labels`,
`manage_monitored` and `manage_notification_filters` require approval the
same way (their `list` action does not)."

**Panel.**

- `turnState.buildApprovalContext` reads `previewId` from
  `data.preview_id` (preview results) or `call.data.preview_id`
  (`confirmation_required` errors) and sets
  `destructive = input.action ∈ {"delete", "remove"} || plan has remove_card/remove_section/delete_view`.
- `planIdsFromRecords` collects `preview_id` alongside `plan_id`/`card_id`.
- `ApprovalCard` gets a checkbox per item when there is more than one,
  all checked by default; unchecking removes that item's id from the Apply
  payload. The Apply label reads "Apply N of M" when a subset is selected;
  Apply is disabled at zero.
- `handleApply` builds the message: "Yes, apply the change." /
  "Yes, apply all N changes." / "Yes, apply these N of M changes: <descriptions>. Do not apply the others."
- Destructive styling: when any selected item is destructive, the Apply
  button uses the error colour (`--color-error-soft` background,
  `--color-error` border and text) and a line above the buttons reads
  "Deletes: <target list>". `DashboardPlanCard` applies the same rule when
  any op is destructive.

### 4.5 Error handling

- `VerificationLog` I/O errors are logged and never fail a write; the
  in-memory list stays authoritative for the process.
- `complete()` on an unknown id logs and returns None.
- A `not_approved` result is an ordinary tool error; the loop continues.
- Fingerprint mismatch caused by the model re-ordering or re-typing a
  parameter is a `not_approved`; the prompt tells the model to re-preview.
- Nightly job: all new saves are inside the existing `try`; a failure to
  record the failure is logged.

### 4.6 Testing

Python:
- `test_verifications.py`: start/complete/ack/recent/persistence/capacity;
  unknown id.
- `test_rollback.py`: optimistic path records pending then verified/failed;
  `to_dict()` carries `verification` fields.
- `test_scheduler_sync_failure.py`: parse failure → finding + `last_sync_error`;
  next success clears both; "nothing to do" clears the error without a finding.
- `test_approval.py`: fingerprint stable across key order, ignores
  `dry_run`, changes with any param.
- `test_executor.py`: dry-run result gains `preview_id`; approved id
  allows; missing id → `not_approved`; `free_actions` bypass; `approval_key`
  path; `confirmation_required` carries `preview_id`.
- `test_context_assembler.py`: verifications surface present/absent.
- Route tests: status carries `verifications`, `model`, `provider`,
  `last_sync_error`; ack endpoint.
- Prompt version bumps to 0.8.0 with a `PROMPT_CHANGELOG` entry.

UI (Vitest): `turnState` collects preview ids and marks destructive;
`cost.ts` provider fallback and `contextWindowFor`.
`tsc -b --noEmit` clean.

## 5. Rollout

Changelog `## [1.8.0]`; merge = release via `scripts/release.sh 1.8.0`.
Existing approvals in flight are unaffected (plan/card ids still work);
a turn approved under 1.7 semantics that is replayed after upgrade simply
returns `not_approved` and the model re-previews.
