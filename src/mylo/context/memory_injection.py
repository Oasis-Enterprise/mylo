"""Build the memory section of the system prompt.

Spec §6.5 — Layer 3 memory injection. We always include
``household`` and ``preferences`` and conditionally include other
sections based on simple keyword matching on the latest user turn.

M8a scope: always-include sections plus the scratchpad. Full
conditional selection (known_issues, patterns, baselines gated on
keywords) is left to M4c's selective-memory logic; this module does
the minimum needed for user notes to actually influence replies.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mylo.memory.schema import Conflict, MemoryFile
from mylo.memory.scratchpad import read_scratchpad, summarize_entries
from mylo.validators.yaml_parser import dump_yaml

_ALL_SECTIONS: frozenset[str] = frozenset(
    {"now", "household", "preferences", "notes", "known_issues", "conflicts", "scratchpad"}
)


def build_memory_section(
    memory: MemoryFile,
    *,
    mylo_data_dir: Path,
    scratchpad_limit: int = 50,
    timezone: str | None = None,
    sections: set[str] | frozenset[str] | None = None,
) -> str:
    """Return a text block to splice into the system prompt after the
    static identity/security rules. Empty string when there's nothing
    to say so we don't burn tokens on blank structure.

    ``sections`` filters which parts render. When None, everything
    renders (backward compatible). The assembler passes the result
    of :func:`mylo.context.selector.select_sections`.
    """
    enabled = frozenset(sections) if sections is not None else _ALL_SECTIONS

    parts: list[str] = []

    if "now" in enabled:
        # Always include current time so time-based memory rules ("don't
        # turn on X after 8pm") can fire without a tool call.
        parts.append(_render_now(timezone))

    if "household" in enabled:
        household_text = _render_household(memory)
        if household_text:
            parts.append(household_text)

    if "preferences" in enabled:
        preferences_text = _render_preferences(memory)
        if preferences_text:
            parts.append(preferences_text)

    if "notes" in enabled and memory.notes:
        parts.append(_render_notes(memory))

    if "known_issues" in enabled:
        issues_text = _render_known_issues(memory)
        if issues_text:
            parts.append(issues_text)

    if "conflicts" in enabled:
        pending = memory.pending_conflicts()
        if pending:
            parts.append(_render_conflicts(pending))

    if "scratchpad" in enabled:
        scratchpad = read_scratchpad(mylo_data_dir, limit=scratchpad_limit)
        if scratchpad:
            summary = summarize_entries(scratchpad)
            parts.append("RECENT USER NOTES (not yet reconciled; treat as current):\n" + summary)

    if not parts:
        return ""

    return "YOUR MEMORY OF THIS HOME:\n\n" + "\n\n".join(parts)


# ─── Renderers ──────────────────────────────────────────────────────────────


def _render_now(timezone: str | None) -> str:
    tz: ZoneInfo | None = None
    if timezone:
        try:
            tz = ZoneInfo(timezone)
        except Exception:
            tz = None
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    return (
        f"CURRENT TIME: {now.strftime('%A, %B %d, %Y — %-I:%M %p %Z')} "
        f"(ISO: {now.isoformat(timespec='seconds')})"
    )


def _render_household(memory: MemoryFile) -> str:
    if not memory.household.members:
        return ""
    lines = ["Household:"]
    for m in memory.household.members:
        bits = [f"- {m.name} ({m.role})"]
        if m.presence_entity:
            bits.append(f"  presence: {m.presence_entity}")
        for note in m.notes:
            bits.append(f"  - {note}")
        lines.append("\n".join(bits))
    return "\n".join(lines)


def _render_preferences(memory: MemoryFile) -> str:
    prefs = memory.preferences
    parts: list[str] = []
    if prefs.dashboard.card_style or prefs.dashboard.notes:
        parts.append(
            f"Dashboards: style={prefs.dashboard.card_style or '—'}, "
            f"{prefs.dashboard.notes or ''}".strip()
        )
    if prefs.naming.convention:
        parts.append(f"Naming convention: {prefs.naming.convention}")
    if prefs.alerts.sensitivity or prefs.alerts.quiet_hours:
        parts.append(
            f"Alerts: sensitivity={prefs.alerts.sensitivity or '—'}, "
            f"quiet_hours={prefs.alerts.quiet_hours or '—'}"
        )
    if not parts:
        return ""
    return "Preferences:\n" + "\n".join(f"- {p}" for p in parts)


def _render_notes(memory: MemoryFile) -> str:
    lines = ["Notes:"]
    for note in memory.notes:
        scope_bits = [b for b in (note.entity, note.area, note.scope) if b]
        scope_str = f" [{', '.join(scope_bits)}]" if scope_bits else ""
        lines.append(f"- {note.id}{scope_str}: {note.content}")
    return "\n".join(lines)


def _render_known_issues(memory: MemoryFile) -> str:
    active = [i for i in memory.known_issues if i.status == "active"]
    if not active:
        return ""
    lines = ["Known issues:"]
    for issue in active:
        lines.append(f"- {issue.id}: {issue.description}")
        if issue.suggested_fix:
            lines.append(f"  fix: {issue.suggested_fix}")
    return "\n".join(lines)


def _render_conflicts(conflicts: list[Conflict]) -> str:
    lines = ["PENDING CONFLICTS (unresolved; mention if relevant):"]
    for c in conflicts:
        subject = c.subject.get("entity") or c.subject.get("area") or c.id
        lines.append(f"- {c.id} about {subject}")
        if c.claim_a:
            lines.append(f"  claim A ({c.claim_a.source}): {c.claim_a.content}")
        if c.claim_b:
            lines.append(f"  claim B ({c.claim_b.source}): {c.claim_b.content}")
    return "\n".join(lines)


# ─── Debug helper ───────────────────────────────────────────────────────────


def raw_memory_yaml(memory: MemoryFile) -> str:
    """Dump the full memory as YAML — useful for debug logs / the
    Memory tab's "show raw" button (M8c).
    """
    return dump_yaml(memory.model_dump(exclude_none=False))
