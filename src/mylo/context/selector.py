"""Layer 3 — selective memory injection (spec §6.5).

Not every memory section belongs in every turn. ``household`` and
``preferences`` are always included (small, load-bearing for
personalization). The rest are gated on keywords in the latest user
turn, plus always-on inclusion for pending conflicts.

Keyword matching is lightweight — a 10-line rule set, not semantic
search. The failure mode is loading a section the model ignores,
which is cheap; the alternative (semantic embedding per turn) would
add latency + cost for marginal gain on a home-automation assistant.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mylo.memory.schema import MemoryFile


# Section names used throughout the memory layer and by the memory_injection
# renderer. "now" is always on (current time), "scratchpad" too.
ALWAYS_ON: frozenset[str] = frozenset({"now", "household", "preferences", "scratchpad"})

# Keyword rules from spec §6.5.
_TRIGGER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "known_issues": ("problem", "issue", "broken", "not working", "debug", "fix", "error"),
    "patterns": ("usually", "normally", "pattern", "schedule", "typical", "every"),
    "baselines": ("energy", "usage", "anomaly", "unusual", "consumption", "kwh"),
    "rejected": ("rename", "suggest", "recommend", "dashboard", "automation"),
}


def select_sections(
    conversation_text: str,
    memory: MemoryFile | None = None,
    *,
    include_notes: bool = True,
) -> set[str]:
    """Return the set of memory section names to render for this turn.

    ``conversation_text`` should be the latest user turn plus a small
    tail of prior context. The assembler passes the last turn since
    that's the "what are we talking about now" signal — keyword
    matches on old turns would linger and over-include.
    """
    chosen: set[str] = set(ALWAYS_ON)
    if include_notes:
        chosen.add("notes")

    text = conversation_text.lower()
    for section, keywords in _TRIGGER_KEYWORDS.items():
        if _any_match(text, keywords):
            chosen.add(section)

    # Pending conflicts: always include if any exist, regardless of text.
    if memory is not None and memory.pending_conflicts():
        chosen.add("conflicts")

    return chosen


def _any_match(text: str, keywords: tuple[str, ...]) -> bool:
    """Whole-word match (with ``\\b``) so "pattern" doesn't trigger
    on "patterns of behavior" inside URLs/tool names and so short
    keywords like "fix" don't match "fixture" accidentally.
    """
    for kw in keywords:
        if " " in kw:
            # Multi-word phrase: look for the literal substring.
            if kw in text:
                return True
        elif re.search(rf"\b{re.escape(kw)}\b", text):
            return True
    return False
