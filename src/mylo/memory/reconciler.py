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

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from mylo.ha.registries import Registries
from mylo.logging_setup import get_logger
from mylo.memory.pruner import PruneReport, plan_prune
from mylo.memory.schema import MemoryFile, empty_memory
from mylo.memory.scratchpad import ScratchpadEntry, read_scratchpad
from mylo.memory.store import MemoryStore
from mylo.validators.yaml_parser import dump_yaml, load_yaml

log = get_logger(__name__)


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
    scratchpad = read_scratchpad(mylo_data_dir)
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
    user_msg = _build_user_payload(memory, scratchpad, diff)

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
5. For new state (entities/devices/areas), ONLY add them to
   `monitored_entities` or `household.shared` if there's evidence the
   user cares. Mere existence of a new entity is not evidence.
6. Output ONLY a YAML document. No prose before or after. No code
   fences unless you're showing them literally inside a string.
7. The output must validate against the schema the user provides.
8. Preserve every key present in the input memory unless a rule above
   says to change it.

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
    memory_yaml = dump_yaml(memory.model_dump(exclude_none=False))
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
    diff_yaml = dump_yaml(
        {
            "new_entities": diff.new_entities,
            "removed_entities": diff.removed_entities,
            "new_devices": diff.new_devices,
            "removed_devices": diff.removed_devices,
            "new_areas": diff.new_areas,
            "removed_areas": diff.removed_areas,
        }
    )

    return (
        "Current memory (context.yaml):\n\n"
        f"{memory_yaml}\n"
        "New scratchpad entries since last sync:\n\n"
        f"{scratchpad_yaml}\n"
        "Home Assistant state diff since last sync:\n\n"
        f"{diff_yaml}\n"
        "Produce the updated memory YAML now."
    )


# ─── Output parsing ──────────────────────────────────────────────────────────


_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", re.DOTALL)


def _parse_reconciler_output(text: str) -> MemoryFile:
    """Pull a YAML doc out of the model's reply and validate it.

    Haiku follows "no code fences" instructions most of the time but
    occasionally wraps output anyway — strip the fence if present.
    """
    stripped = text.strip()
    fence_match = _CODE_FENCE_RE.match(stripped)
    if fence_match:
        stripped = fence_match.group(1)

    parsed = load_yaml(stripped)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected YAML mapping, got {type(parsed).__name__}")

    return MemoryFile.model_validate(parsed)


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
