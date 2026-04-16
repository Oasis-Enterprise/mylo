"""Layer 4 — task-type detection (spec §6.6).

Classifies the user's current intent into one of a small set of task
types (``dashboard``, ``automation``, ``troubleshoot``,
``entity_management``). A match means the assembler loads the
corresponding few-shot reference examples so the model has
well-formed YAML to crib from.

Keyword scoring, not classification. Threshold of 2 matches so a
stray word doesn't trigger a whole reference dump. Returns None when
no task is confidently detected — chats about current state shouldn't
haul in automation examples.
"""

from __future__ import annotations

import re
from typing import Literal

TaskType = Literal["dashboard", "automation", "troubleshoot", "entity_management"]

# Each entry is (pattern, weight). Patterns are regex so multi-word
# phrases work and "turn on when" can catch the automation intent
# without triggering on "turn on the kitchen light".
_TASK_PATTERNS: dict[TaskType, tuple[tuple[str, int], ...]] = {
    "dashboard": (
        (r"\bdashboard\b", 2),
        (r"\blovelace\b", 2),
        (r"\b(card|cards)\b", 1),
        (r"\bview\b", 1),
        (r"\blayout\b", 1),
        (r"\bgauge\b", 1),
        (r"\bgraph\b", 1),
        (r"\bdisplay\b", 1),
    ),
    "automation": (
        (r"\bautomation\b", 2),
        (r"\btrigger(s|ed)?\b", 1),
        (r"\bwhen\s+\w+\s+(is|are|turns?|becomes?)", 1),
        (r"\bif\s+.+\s+then\b", 2),
        (r"\bschedule\b", 1),
        (r"\broutine\b", 1),
        (r"\bturn\s+on\s+when\b", 2),
        (r"\bevery\s+(morning|evening|night|day)\b", 2),
    ),
    "troubleshoot": (
        (r"\bnot working\b", 2),
        (r"\bbroken\b", 2),
        (r"\berror\b", 1),
        (r"\boffline\b", 1),
        (r"\bunavailable\b", 1),
        (r"\bdebug\b", 2),
        (r"\blogs?\b", 1),
        (r"\bwhy\s+(is|isn'?t|won'?t|does|doesn'?t)\b", 2),
    ),
    "entity_management": (
        (r"\brename\b", 2),
        (r"\borganize\b", 2),
        (r"\bclean\s*up\b", 2),
        (r"\bnaming\b", 1),
        (r"\blabel\b", 1),
        (r"\barea\b", 1),
        (r"\bassign\b", 1),
        (r"\bmove\s+.+\s+to\b", 1),
    ),
}

# Below this total score, don't trigger — avoids pulling reference
# packs on every chat about current state.
_MIN_SCORE = 2


def detect_task_type(text: str) -> TaskType | None:
    """Return the highest-scoring task type, or None if no confident match.

    Ties break toward whichever task type appears first in the dict —
    deterministic and avoids flapping between runs.
    """
    if not text:
        return None
    lowered = text.lower()
    scores: dict[TaskType, int] = {}
    for task, patterns in _TASK_PATTERNS.items():
        score = 0
        for pattern, weight in patterns:
            if re.search(pattern, lowered):
                score += weight
        if score >= _MIN_SCORE:
            scores[task] = score
    if not scores:
        return None
    best = max(scores.items(), key=lambda item: item[1])
    return best[0]
