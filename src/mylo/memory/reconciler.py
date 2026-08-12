# Copyright 2026 Maxwell Monson / Oasis Enterprise LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Reconciler — merge scratchpad + state diff into ``context.yaml``.

The tool-loop LLM (Sonnet) writes short inline notes to
``scratchpad.yaml`` during conversations. The reconciler (Haiku,
cheaper) runs on demand or on a schedule: it reads the scratchpad and
HA registry, compares against the current memory, and produces an
updated ``MemoryFile``.

Spec §3.5 sets the rules:

* Never remove user-confirmed information
* Contradictions → emit a :class:`Conflict`, do not silently overwrite
* Merge redundant notes
* Update ``last_referenced`` for items that were mentioned
* Output only valid YAML matching the schema

This module is LLM-agnostic in test — you can pass a stub provider
that returns a canned YAML string. Production wiring uses
:class:`mylo.llm.anthropic_provider.AnthropicProvider` via the
existing app-level singleton.
"""

from __future__ import annotations

import copy
import re
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from mylo.context.tokens import estimate_tokens
from mylo.ha.registries import Registries
from mylo.logging_setup import get_logger
from mylo.memory.pruner import PruneReport, plan_prune
from mylo.memory.schema import MemoryFile, Note, empty_memory
from mylo.memory.scratchpad import ScratchpadEntry, read_scratchpad, trim_scratchpad
from mylo.memory.store import MemoryStore
from mylo.validators.yaml_parser import dump_yaml, load_yaml, load_yaml_lenient

log = get_logger(__name__)


# Machine-owned memory sections: pure state the reconciler LLM cannot
# meaningfully merge. They are never sent to the model (on large
# instances they bloat the prompt past the 200k context window) and
# never read back from its output — always carried over verbatim from
# the prior memory. See _carry_over_machine_sections.
_MACHINE_SECTIONS: tuple[str, ...] = (
    "monitored_entities",
    "notification_suppressions",
    "suggestions",
    "pending_actions",
    "finding_cooldowns",
    "baselines",
)

# How many entity/device/area IDs from the state diff to put in the
# prompt. The reconciler doesn't act on them in bulk (system prompt
# rule 5), so a sample + total count is enough; the full lists can be
# thousands of IDs on a large instance.
_DIFF_SAMPLE_CAP = 50

# Approximate ceiling for the assembled user payload. Anthropic's hard
# limit is 200k tokens for input + output combined; we stay well under
# to leave room for the system prompt and the model's reply. Sizing uses
# the shared conservative estimator (mylo.context.tokens.estimate_tokens).
_PAYLOAD_TOKEN_BUDGET = 150_000

# Newest scratchpad entries sent per sync. A streak of failed merges
# never drains the file, so without a bound the payload grew every
# night; the overflow stays on disk (and trim_scratchpad archives it)
# until a successful merge drains the file.
_SCRATCHPAD_RECONCILE_LIMIT = 300


# ─── Provider interface (narrow) ─────────────────────────────────────────────


class ReconcileProvider(Protocol):
    """Minimum surface the reconciler needs.

    Kept narrower than :class:`mylo.llm.provider.Provider` so tests
    can stub one method without faking a tool-calling model.
    """

    async def message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int = 4096,
    ) -> Any: ...


# ─── Inputs / outputs ────────────────────────────────────────────────────────


@dataclass(slots=True)
class StateDiff:
    """Summary of HA changes since the last sync."""

    new_entities: list[str] = field(default_factory=list)
    removed_entities: list[str] = field(default_factory=list)
    new_devices: list[str] = field(default_factory=list)
    removed_devices: list[str] = field(default_factory=list)
    new_areas: list[str] = field(default_factory=list)
    removed_areas: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_entities
            or self.removed_entities
            or self.new_devices
            or self.removed_devices
            or self.new_areas
            or self.removed_areas
        )


@dataclass(slots=True)
class ReconcileResult:
    """What the reconciler produced.

    ``updated`` is None when the reconciler saw no work to do — the
    caller should skip the save. ``conflicts_added`` counts conflicts
    that weren't in the prior memory; the UI uses this to decide
    whether to surface a review banner.
    """

    updated: MemoryFile | None
    summary: str
    conflicts_added: int
    prune_report: PruneReport
    raw_output: str = ""  # the model's raw YAML — preserved for debug

    @property
    def changed(self) -> bool:
        return self.updated is not None


# ─── Payload compaction ──────────────────────────────────────────────────────


def _note_protected(note: Note) -> bool:
    """User-confirmed and critical notes are never compacted out."""
    return note.source == "user_confirmed" or note.metadata.priority == "critical"


def compact_payload_sections(
    memory: MemoryFile, *, budget_tokens: int
) -> tuple[MemoryFile, str, dict[str, list[Any]]]:
    """Return a copy of ``memory`` whose serialized size fits ``budget_tokens``,
    compacting the most expendable sections first (patterns, then plain
    notes). Critical items (user_confirmed / critical-priority notes) are
    never dropped.

    Returns ``(compacted_memory, marker, dropped)`` where ``dropped`` maps
    each section to the items removed — the caller re-attaches these after
    the merge so compaction never loses data. Drops in chunks to keep this
    O(rounds times n), not O(n squared).
    """
    work = copy.deepcopy(memory)
    dropped: dict[str, list[Any]] = {"notes": [], "patterns": []}
    if estimate_tokens(work.model_dump_json()) <= budget_tokens:
        return work, "", dropped

    counts: Counter[str] = Counter()
    for section in ("patterns", "notes"):
        items = list(getattr(work, section, []))
        if section == "notes":
            protected = [n for n in items if _note_protected(n)]
            droppable = [n for n in items if not _note_protected(n)]
        else:
            protected = []
            droppable = list(items)
        while droppable and estimate_tokens(work.model_dump_json()) > budget_tokens:
            chunk = max(1, len(droppable) // 10)
            removed = droppable[-chunk:]
            del droppable[-chunk:]
            dropped[section].extend(removed)
            counts[section] += len(removed)
            setattr(work, section, protected + droppable)
        setattr(work, section, protected + droppable)

    marker = "; ".join(f"+{n} {sec} compacted" for sec, n in counts.items())
    return work, marker, dropped


def _reattach_compacted(merged: MemoryFile, dropped: dict[str, list[Any]]) -> None:
    """Re-add items dropped from the payload only to fit context.

    They skipped this pass's reconciliation, so add back any whose id isn't
    already present in the merged result (the LLM never saw them and so
    can't have changed them).
    """
    existing_note_ids = {n.id for n in merged.notes}
    for note in dropped.get("notes", []):
        if note.id not in existing_note_ids:
            merged.notes.append(note)
    existing_pattern_ids = {p.id for p in merged.patterns}
    for pattern in dropped.get("patterns", []):
        if pattern.id not in existing_pattern_ids:
            merged.patterns.append(pattern)


# ─── Public entrypoint ───────────────────────────────────────────────────────


async def run_sync(
    *,
    store: MemoryStore,
    provider: ReconcileProvider | None,
    registries: Registries | None,
    model: str,
    mylo_data_dir: Path,
    now: datetime | None = None,
) -> ReconcileResult:
    """Run one reconciliation pass end-to-end.

    The flow:

    1. Load current memory + scratchpad + registry.
    2. Compute state diff.
    3. If nothing changed: prune-only pass (may still drop stale items).
    4. Otherwise: call the reconciler LLM.
    5. Parse + validate YAML.
    6. Merge protected sections (defense-in-depth against prompt drift).
    7. Plan a prune on the merged result.
    8. Save if anything actually changed.
    """
    current = now or datetime.now(UTC)
    memory = store.current() if store.current() is not None else await store.load()
    trim_scratchpad(mylo_data_dir)
    scratchpad = read_scratchpad(mylo_data_dir, limit=_SCRATCHPAD_RECONCILE_LIMIT)
    diff = _build_state_diff(memory, registries)

    # Prune pass is always computed — we want to report candidates
    # even when we skip the LLM call. apply_prune stays the caller's
    # decision so "auto-prune" and "review" can share this path.
    prune_report = plan_prune(memory, now=current)

    if not scratchpad and not diff.has_changes:
        log.info("memory.sync_skipped", reason="no_scratchpad_no_state_diff")
        return ReconcileResult(
            updated=None,
            summary="no new scratchpad entries or state changes since last sync",
            conflicts_added=0,
            prune_report=prune_report,
        )

    if provider is None:
        # Without a provider we can still land scratchpad as tentative
        # notes + surface the state diff, but we can't do semantic
        # merging. Emit a degraded result rather than crashing so the
        # endpoint is still useful offline.
        log.warning("memory.reconciler_no_provider — falling back to scratchpad-only merge")
        updated = _fallback_merge(memory, scratchpad, diff, current)
        updated.last_sync = current.replace(microsecond=0).isoformat()
        return ReconcileResult(
            updated=updated,
            summary=(
                f"degraded: merged {len(scratchpad)} scratchpad entries + "
                f"{len(diff.new_entities)} new entities without LLM"
            ),
            conflicts_added=0,
            prune_report=plan_prune(updated, now=current),
        )

    prompt = _build_system_prompt()
    # Compact the payload so it always fits the window — drop the most
    # expendable notes/patterns from what the LLM sees (re-attached
    # untouched after the merge so nothing is lost), rather than skipping
    # the merge entirely on a large memory.
    payload_budget = _PAYLOAD_TOKEN_BUDGET - estimate_tokens(prompt)
    memory_for_payload, compaction_marker, dropped = compact_payload_sections(
        memory, budget_tokens=payload_budget
    )
    user_msg = _build_user_payload(memory_for_payload, scratchpad, diff)
    if compaction_marker:
        user_msg += f"\n\n# NOTE: memory compacted to fit context — {compaction_marker}"
        log.warning("memory.reconciler_compacted", detail=compaction_marker)

    # Final hard backstop: if even the compacted payload won't fit (e.g.
    # protected notes alone exceed the window), skip the LLM merge and
    # degrade gracefully rather than letting the API 400.
    est_tokens = estimate_tokens(prompt) + estimate_tokens(user_msg)
    if est_tokens > _PAYLOAD_TOKEN_BUDGET:
        log.error(
            "memory.reconciler_payload_too_large",
            est_tokens=est_tokens,
            budget=_PAYLOAD_TOKEN_BUDGET,
        )
        return ReconcileResult(
            updated=None,
            summary=(
                f"memory too large to reconcile (~{est_tokens} tokens > "
                f"{_PAYLOAD_TOKEN_BUDGET} budget); skipped LLM merge. "
                "Prune notes/conflicts and retry."
            ),
            conflicts_added=0,
            prune_report=prune_report,
        )

    response = await provider.message(
        system=prompt,
        messages=[{"role": "user", "content": user_msg}],
        tools=[],
        model=model,
        max_tokens=8192,
    )

    raw_text = getattr(response, "text", "") or ""
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

    merged = _protect_user_sections(memory, proposed, scratchpad, current)
    _carry_over_machine_sections(memory, merged)
    # Restore items that were dropped from the payload only to fit context —
    # they skipped this pass's reconciliation but must not be lost.
    _reattach_compacted(merged, dropped)
    merged.last_sync = current.replace(microsecond=0).isoformat()

    conflicts_added = len(merged.pending_conflicts()) - len(memory.pending_conflicts())
    final_prune = plan_prune(merged, now=current)

    return ReconcileResult(
        updated=merged,
        summary=_summarize_changes(memory, merged, diff, scratchpad),
        conflicts_added=max(conflicts_added, 0),
        prune_report=final_prune,
        raw_output=raw_text,
    )


# ─── Prompt assembly ────────────────────────────────────────────────────────


RECONCILER_SYSTEM_PROMPT = """You are a memory reconciliation agent for a
Home Assistant automation helper called Mylo. Your job: merge fresh
observations into an existing YAML memory file without losing anything
important.

RULES — follow exactly:
1. NEVER remove user-confirmed information (source: user_confirmed or
   priority: critical items). Even if an observation seems to
   contradict them, leave the user-confirmed item and emit a conflict.
2. When new information contradicts existing memory, add an entry to
   the `conflicts` list with both claim_a (from existing memory) and
   claim_b (from new observation). Do NOT overwrite the existing item.
3. Merge redundant notes — if two notes describe the same thing,
   combine them into one with a merged `content` and the earlier
   `created` timestamp.
4. Update `last_referenced` and `reference_count` for any items the
   scratchpad mentioned.
5. The Home Assistant state diff is informational. Do NOT record a new
   entity/device/area anywhere just because it exists — only capture it
   (e.g. in `household.shared` or a note) when the scratchpad shows the
   user cares about it. Monitoring lists and other machine state are
   managed elsewhere and are not part of this document.
6. Output ONLY a YAML document. No prose before or after. No code
   fences unless you're showing them literally inside a string.
7. The output must validate against the schema the user provides.
8. Preserve every key present in the input memory unless a rule above
   says to change it.

Note shape (IMPORTANT — do not copy the scratchpad ``scope: {...}``
dict into Notes; Notes have separate fields):

  notes:
    - id: note_<short>
      content: "<what to remember>"
      entity: "<entity_id>" OR null
      area: "<area_slug>" OR null
      scope: "general" OR null
      source: conversation | observation | user_confirmed
      added: "<ISO>"
      metadata:
        created: "<ISO>"
        last_referenced: "<ISO>"
        reference_count: 1
        source: conversation | observation | user_confirmed
        priority: normal | critical | low

Conflict entry shape:

  conflicts:
    - id: conflict_<short-hash>
      type: <"contradiction" | "duplicate" | "stale">
      subject: {entity: "<entity_id>"} OR {area: "<area>"}
      claim_a:
        content: "<existing claim>"
        source: "memory"
        date: "<ISO>"
      claim_b:
        content: "<new observation>"
        source: "scratchpad" OR "ha_state"
        date: "<ISO>"
      status: pending_review

If no conflicts: omit the entry or leave the existing `conflicts`
list intact.

Output a single YAML document — the full updated memory file.
"""


def _build_system_prompt() -> str:
    return RECONCILER_SYSTEM_PROMPT


def _build_user_payload(
    memory: MemoryFile,
    scratchpad: list[ScratchpadEntry],
    diff: StateDiff,
) -> str:
    # Only the sections the LLM actually reconciles go in the prompt.
    # Machine-owned state (baselines, pending_actions, monitored_entities,
    # …) is excluded — it can't be merged semantically and would blow the
    # context window on large instances. It's reattached verbatim after
    # the call (see _carry_over_machine_sections).
    reconcilable = {
        k: v for k, v in memory.model_dump(exclude_none=False).items() if k not in _MACHINE_SECTIONS
    }
    memory_yaml = dump_yaml(reconcilable)
    scratchpad_yaml = dump_yaml(
        [
            {
                "type": e.type,
                "content": e.content,
                "scope": e.scope,
                "recorded": e.recorded,
                "confidence": e.confidence,
                "conversation_id": e.conversation_id,
            }
            for e in scratchpad
        ]
    )
    diff_yaml = dump_yaml(_summarize_diff(diff))

    return (
        "Current memory (context.yaml):\n\n"
        f"{memory_yaml}\n"
        "New scratchpad entries since last sync:\n\n"
        f"{scratchpad_yaml}\n"
        "Home Assistant state diff since last sync:\n\n"
        f"{diff_yaml}\n"
        "Produce the updated memory YAML now."
    )


def _summarize_diff(diff: StateDiff) -> dict[str, Any]:
    """Render the state diff with each list sampled to a cap.

    The reconciler only needs to know *that* things changed and roughly
    what; it never processes the full lists (which can be thousands of
    IDs). Each field becomes ``{count, items}`` or, when over the cap,
    ``{count, sample, omitted}``.
    """

    def field_summary(items: list[str]) -> dict[str, Any]:
        if len(items) <= _DIFF_SAMPLE_CAP:
            return {"count": len(items), "items": items}
        return {
            "count": len(items),
            "sample": items[:_DIFF_SAMPLE_CAP],
            "omitted": f"{len(items) - _DIFF_SAMPLE_CAP} more not shown",
        }

    return {
        "new_entities": field_summary(diff.new_entities),
        "removed_entities": field_summary(diff.removed_entities),
        "new_devices": field_summary(diff.new_devices),
        "removed_devices": field_summary(diff.removed_devices),
        "new_areas": field_summary(diff.new_areas),
        "removed_areas": field_summary(diff.removed_areas),
    }


def _carry_over_machine_sections(original: MemoryFile, merged: MemoryFile) -> None:
    """Copy machine-owned sections from the prior memory onto the merged
    result, in place.

    These sections are never shown to the LLM, so its output omits them
    (they'd validate as empty defaults). They're authoritative state
    owned by the monitor/scheduler, not the reconciler — restore them
    verbatim so a sync can neither drop nor rewrite them.

    ``merged`` ends up sharing these objects with ``original`` by
    reference. That's safe here: ``merged`` replaces the store's cached
    memory on save and ``original`` is dropped, and callers rebind these
    sections (e.g. nightly ``baselines`` recompute) rather than mutating
    them in place. Deep-copy if that invariant ever changes.
    """
    for field_name in _MACHINE_SECTIONS:
        setattr(merged, field_name, getattr(original, field_name))


# ─── Output parsing ──────────────────────────────────────────────────────────


_FENCE_OPEN_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding markdown code fence if present.

    Line-wise and tolerant: if the first line is an opening fence
    (``` ```yaml ``` or ``` ``` ```), drop it, plus a trailing closing
    ``` ``` ``` if there is one. The old whole-string regex required BOTH
    fences and failed when the model emitted ``` ```yaml ``` with the
    close missing or trailing junk — leaving the backtick to break the
    YAML parser.
    """
    lines = text.split("\n")
    if not lines or not _FENCE_OPEN_RE.match(lines[0]):
        return text
    body = lines[1:]
    if body and _FENCE_CLOSE_RE.match(body[-1]):
        body = body[:-1]
    return "\n".join(body)


def _parse_reconciler_output(text: str) -> MemoryFile:
    """Pull a YAML doc out of the model's reply and validate it.

    Haiku follows "no code fences" instructions most of the time but
    occasionally wraps output anyway — strip the fence if present.

    LLM-generated YAML fails in recurring, repairable ways: unquoted
    strings containing colons (``content: specs (cold): 36 psi``),
    duplicate mapping keys (``rejected: []`` emitted twice \u2014 ruamel's
    strict round-trip loader hard-fails on those), and shape drift the
    pydantic validators repair. The parse is a ladder \u2014 each rung gets
    a full parse+validate attempt, and only when every rung fails does
    the night's merge get skipped:

        1. strict load
        2. strict load after quoting ambiguous colons
        3. lenient load (duplicate keys allowed, first occurrence wins)
        4. lenient load after quoting ambiguous colons
    """
    # Strip BOM, zero-width characters, and other invisible unicode
    # that LLMs occasionally emit and that break YAML parsers.
    stripped = text.strip().lstrip("\ufeff\u200b\u200c\u200d\u2060\ufffe")
    # Also strip any non-printable ASCII at the start.
    while stripped and (ord(stripped[0]) < 32 or ord(stripped[0]) == 127):
        stripped = stripped[1:]

    stripped = _strip_code_fence(stripped)

    attempts: list[tuple[str, Callable[[str], Any]]] = [
        (stripped, load_yaml),
        (_fix_unquoted_colons(stripped), load_yaml),
        (stripped, load_yaml_lenient),
        (_fix_unquoted_colons(stripped), load_yaml_lenient),
    ]

    last_exc: Exception | None = None
    for candidate, loader in attempts:
        try:
            parsed = loader(candidate)
            if not isinstance(parsed, dict):
                raise ValueError(f"expected YAML mapping, got {type(parsed).__name__}")
            return MemoryFile.model_validate(parsed)
        except Exception as exc:
            last_exc = exc

    assert last_exc is not None
    raise last_exc


# Matches a YAML line like `    content: some text (cold): more text`
# where the value portion contains an unquoted colon. The first colon
# after the key is the legitimate key:value separator; subsequent
# colons in the value need the whole value quoted.
_YAML_VALUE_RE = re.compile(r"^(\s*\w[\w\s]*:\s*)(.+)$")


def _fix_unquoted_colons(text: str) -> str:
    """Wrap YAML string values that contain colons in double quotes.

    LLMs frequently produce lines like:
        content: Tire specs (cold): Front 36 psi, Rear 42 psi.
    which YAML parsers reject because the second colon looks like a
    nested mapping. This wraps the value portion in quotes so the
    parser sees a single string value.
    """
    lines = text.split("\n")
    fixed: list[str] = []
    for line in lines:
        match = _YAML_VALUE_RE.match(line)
        if match:
            key_part = match.group(1)  # e.g. "    content: "
            value_part = match.group(2)  # e.g. "Tire specs (cold): ..."
            # Only fix if the value has an additional colon AND isn't
            # already quoted AND isn't a YAML structure (list/dict).
            if ":" in value_part and not value_part.startswith(("'", '"', "[", "{", "|", ">")):
                # Escape any existing double quotes in the value.
                escaped = value_part.replace('"', '\\"')
                fixed.append(f'{key_part}"{escaped}"')
                continue
        fixed.append(line)
    return "\n".join(fixed)


# ─── Merge safeguards ────────────────────────────────────────────────────────


def _protect_user_sections(
    original: MemoryFile,
    proposed: MemoryFile,
    scratchpad: list[ScratchpadEntry],
    now: datetime,
) -> MemoryFile:
    """Enforce the "never remove user data" rule client-side.

    The LLM is instructed to preserve user data, but we belt-and-
    suspenders it: any user_confirmed note or active critical item
    present in ``original`` but missing from ``proposed`` is re-
    added. This also guarantees household members are never dropped.
    """
    protected_notes = {
        n.id: n
        for n in original.notes
        if n.source == "user_confirmed" or n.metadata.priority == "critical"
    }
    proposed_ids = {n.id for n in proposed.notes}
    missing = [n for nid, n in protected_notes.items() if nid not in proposed_ids]
    if missing:
        log.warning(
            "memory.reconciler_dropped_protected_notes",
            count=len(missing),
            ids=[n.id for n in missing],
        )
        proposed.notes.extend(missing)

    # Same for active known_issues: reconciler must not silently drop.
    active_issues = {i.id: i for i in original.known_issues if i.status == "active"}
    proposed_issue_ids = {i.id for i in proposed.known_issues}
    for iid, issue in active_issues.items():
        if iid not in proposed_issue_ids:
            log.warning("memory.reconciler_dropped_active_issue", id=iid)
            proposed.known_issues.append(issue)

    # Household is the single most load-bearing section for personalization.
    # If the LLM wiped it, restore from original.
    if not proposed.household.members and original.household.members:
        log.warning("memory.reconciler_dropped_household — restoring")
        proposed.household = original.household

    # Preferences likewise.
    if _preferences_empty(proposed) and not _preferences_empty(original):
        log.warning("memory.reconciler_dropped_preferences — restoring")
        proposed.preferences = original.preferences

    return proposed


def _preferences_empty(memory: MemoryFile) -> bool:
    prefs = memory.preferences
    return not any(
        [
            prefs.dashboard.card_style,
            prefs.dashboard.layout_preference,
            prefs.dashboard.theme,
            prefs.dashboard.notes,
            prefs.naming.convention,
            prefs.alerts.sensitivity,
            prefs.alerts.quiet_hours,
            prefs.alerts.channels,
        ]
    )


# ─── Fallback path (no LLM) ──────────────────────────────────────────────────


def _fallback_merge(
    memory: MemoryFile,
    scratchpad: list[ScratchpadEntry],
    diff: StateDiff,
    now: datetime,
) -> MemoryFile:
    """Minimal non-LLM merge — appends scratchpad as tentative notes.

    Used when the API key is missing. We still want /api/memory/sync
    to do *something* useful so the user can verify plumbing works.
    """
    data = memory.model_dump()
    existing_contents = {n.get("content") for n in data.get("notes") or []}
    for entry in scratchpad:
        if entry.content in existing_contents:
            continue
        note_id = f"note_{uuid.uuid4().hex[:8]}"
        data.setdefault("notes", []).append(
            {
                "id": note_id,
                "content": entry.content,
                "entity": entry.scope.get("entity"),
                "area": entry.scope.get("area"),
                "scope": "general" if entry.scope.get("general") else None,
                "added": entry.recorded or now.isoformat(timespec="seconds"),
                "source": "conversation",
                "metadata": {
                    "created": now.isoformat(timespec="seconds"),
                    "source": entry.type if entry.type in ("observation",) else "conversation",
                    "reference_count": 1,
                },
            }
        )
    return MemoryFile.model_validate(data) if data else empty_memory()


# ─── Summaries + diff ────────────────────────────────────────────────────────


def _build_state_diff(memory: MemoryFile, registries: Registries | None) -> StateDiff:
    """Compare monitored_entities against the current registry.

    We treat ``monitored_entities`` as the high-water mark of "what
    we know existed last time we synced". Everything the registry
    reports that isn't in that list is a new_entity; everything in
    the list that's missing from the registry is a removed_entity.

    Devices and areas don't have a similar persistent list yet, so we
    return empty lists for them. M8c will add `known_devices` /
    `known_areas` tracking if needed.
    """
    if registries is None:
        return StateDiff()

    known = set(memory.monitored_entities)
    live = set(registries.entities.keys())
    new_entities = sorted(live - known) if known else []
    removed_entities = sorted(known - live)

    return StateDiff(new_entities=new_entities, removed_entities=removed_entities)


def _summarize_changes(
    before: MemoryFile,
    after: MemoryFile,
    diff: StateDiff,
    scratchpad: list[ScratchpadEntry],
) -> str:
    parts: list[str] = []
    delta_notes = len(after.notes) - len(before.notes)
    if delta_notes:
        parts.append(f"{delta_notes:+d} notes")
    delta_issues = len(after.known_issues) - len(before.known_issues)
    if delta_issues:
        parts.append(f"{delta_issues:+d} known issues")
    delta_conflicts = len(after.pending_conflicts()) - len(before.pending_conflicts())
    if delta_conflicts:
        parts.append(f"{delta_conflicts:+d} conflicts")
    if diff.new_entities:
        parts.append(f"{len(diff.new_entities)} new HA entities")
    if diff.removed_entities:
        parts.append(f"{len(diff.removed_entities)} removed HA entities")
    parts.append(f"{len(scratchpad)} scratchpad entries merged")
    return ", ".join(parts)
