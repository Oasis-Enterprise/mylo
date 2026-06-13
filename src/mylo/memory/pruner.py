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

"""Deterministic ranked memory pruning (spec §3.4).

The reconciler's LLM merge can add items, but it cannot drop them —
that's this module's job. Pruning is rule-based on purpose: the user
must trust that nothing disappears for mysterious LLM reasons.

Priority order (first pruned first):

1. Expired TTL items
2. Observations never confirmed, older than 90 days
3. Lowest reference_count + oldest last_referenced
4. Resolved known_issues (archive, not delete)
5. Patterns: confidence < 0.5 older than 60 days; behavioral patterns
   not re-confirmed in 21 days (any confidence); then a hard cap of
   MAX_PATTERNS keeping the strongest
6. Rejected suggestions older than 6 months

Never auto-prune:
- notes with source="user_confirmed"
- active known_issues
- anything with priority="critical"
- preferences (whole section)
- household members
- baselines for monitored_entities

The module does not mutate memory by itself — it returns a
:class:`PruneReport` that describes candidates and, when
``apply=True``, a pruned :class:`MemoryFile`. Callers decide whether
to save.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile, Note

log = get_logger(__name__)

DAY = timedelta(days=1)

OBSERVATION_MAX_AGE = timedelta(days=90)
PATTERN_MAX_AGE = timedelta(days=60)
REJECTED_MAX_AGE = timedelta(days=180)
LOW_CONFIDENCE_THRESHOLD = 0.5

# Behavioral patterns are re-confirmed every nightly run within the
# detector's 14-day window. One not confirmed in this long is dead —
# usually because its averaged-time id drifted and a fresh variant took
# over — and is dropped regardless of confidence.
PATTERN_STALE_MAX_AGE = timedelta(days=21)

# Hard ceiling on stored patterns. A real home has at most dozens of
# genuine recurring behaviors; this is a backstop against runaway growth
# (e.g. id drift) that can balloon context.yaml past the LLM window.
MAX_PATTERNS = 200

PruneReason = Literal[
    "ttl_expired",
    "stale_observation",
    "low_reference",
    "resolved_issue",
    "low_confidence_pattern",
    "stale_pattern",
    "pattern_cap_exceeded",
    "old_rejection",
]


@dataclass(slots=True)
class PruneCandidate:
    """One memory item flagged for removal."""

    section: str  # "notes" | "known_issues" | "patterns" | "rejected"
    item_id: str
    reason: PruneReason
    summary: str  # short human-readable description


@dataclass(slots=True)
class PruneReport:
    """Result of a prune pass."""

    candidates: list[PruneCandidate] = field(default_factory=list)
    archived_issues: list[str] = field(default_factory=list)
    pruned: MemoryFile | None = None  # set when apply=True

    @property
    def total(self) -> int:
        return len(self.candidates)


def plan_prune(
    memory: MemoryFile,
    *,
    now: datetime | None = None,
    target_budget: int | None = None,
) -> PruneReport:
    """Walk the memory and rank candidates. Does not mutate.

    ``target_budget`` gates the low-reference sweep (rule 3). Rules 1,
    2, 4, 5, 6 always run — they catch items that are provably stale
    regardless of capacity. Rule 3 only kicks in when the caller
    explicitly says "trim N more". Default is no low-reference trim
    so a casual sync-without-pressure pass stays conservative.
    """
    current = now or datetime.now(UTC)

    candidates: list[PruneCandidate] = []

    # 1. Expired TTL across every section that carries ItemMetadata.
    candidates.extend(_ttl_expired_notes(memory, current))

    # 2. Stale unconfirmed observations.
    candidates.extend(_stale_observations(memory, current))

    # 4. Resolved known_issues (archive).
    resolved_ids = [
        i.id for i in memory.known_issues if i.status == "resolved" and not _is_critical_issue(i)
    ]
    for iid in resolved_ids:
        candidates.append(
            PruneCandidate(
                section="known_issues",
                item_id=iid,
                reason="resolved_issue",
                summary=f"issue {iid} resolved — archive to history",
            )
        )

    # 5. Low-confidence old patterns + stale behavioral patterns, then
    #    a hard cap on whatever survives.
    candidates.extend(_low_confidence_patterns(memory, current))
    candidates.extend(_stale_patterns(memory, current))
    flagged_patterns = {c.item_id for c in candidates if c.section == "patterns"}
    candidates.extend(_excess_patterns(memory, already_flagged=flagged_patterns))

    # 6. Old rejected suggestions.
    candidates.extend(_old_rejections(memory, current))

    # 3. Lowest reference_count + oldest last_referenced (combined
    #    score). Budget-gated: only run when caller explicitly asks
    #    for it — otherwise we'd prune every note on every sync.
    if target_budget is not None and target_budget > 0:
        already_flagged = {(c.section, c.item_id) for c in candidates}
        low_ref = _rank_low_reference_notes(memory, current, exclude=already_flagged)
        candidates.extend(low_ref[:target_budget])

    return PruneReport(
        candidates=candidates,
        archived_issues=resolved_ids,
    )


def apply_prune(memory: MemoryFile, report: PruneReport) -> MemoryFile:
    """Return a copy of ``memory`` with candidates removed.

    Resolved issues are moved out of ``known_issues`` — callers should
    append them to a history file if they want to keep evidence.
    """
    to_drop: dict[str, set[str]] = {
        "notes": set(),
        "known_issues": set(),
        "patterns": set(),
        "rejected": set(),
    }
    for c in report.candidates:
        to_drop.setdefault(c.section, set()).add(c.item_id)

    data = memory.model_dump()
    data["notes"] = [n for n in data.get("notes") or [] if n.get("id") not in to_drop["notes"]]
    data["known_issues"] = [
        i for i in data.get("known_issues") or [] if i.get("id") not in to_drop["known_issues"]
    ]
    data["patterns"] = [
        p for p in data.get("patterns") or [] if p.get("id") not in to_drop["patterns"]
    ]
    data["rejected"] = [
        r for r in data.get("rejected") or [] if r.get("id") not in to_drop["rejected"]
    ]
    return MemoryFile.model_validate(data)


# ─── Rule helpers ───────────────────────────────────────────────────────────


def _is_protected_note(note: Note) -> bool:
    """Notes the user explicitly confirmed, or flagged critical, stay."""
    if note.source == "user_confirmed":
        return True
    if note.metadata.priority == "critical":
        return True
    return note.metadata.source == "user_confirmed"


def _is_critical_issue(issue: object) -> bool:
    """Active/critical issues never archive. We use a duck-typed check
    because Pydantic returns the model, but ``model_dump()`` consumers
    pass dicts — share one helper.
    """
    status = getattr(issue, "status", None) or (isinstance(issue, dict) and issue.get("status"))
    return status == "active"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _ttl_expired_notes(memory: MemoryFile, now: datetime) -> list[PruneCandidate]:
    out: list[PruneCandidate] = []
    for note in memory.notes:
        if _is_protected_note(note):
            continue
        ttl = _parse_iso(note.metadata.ttl)
        if ttl is not None and ttl <= now:
            out.append(
                PruneCandidate(
                    section="notes",
                    item_id=note.id,
                    reason="ttl_expired",
                    summary=f"note {note.id} TTL expired {note.metadata.ttl}",
                )
            )
    return out


def _stale_observations(memory: MemoryFile, now: datetime) -> list[PruneCandidate]:
    cutoff = now - OBSERVATION_MAX_AGE
    out: list[PruneCandidate] = []
    for note in memory.notes:
        if _is_protected_note(note):
            continue
        source = note.metadata.source or note.source
        if source != "observation":
            continue
        # Confirmed observations are promoted to user_confirmed; untouched
        # observations keep their original source.
        created = _parse_iso(note.metadata.created) or _parse_iso(note.added)
        if created is None:
            # No timestamp — treat as fresh; the reconciler should stamp
            # all new items, so untimestamped items are likely user-
            # imported. Conservative: leave alone.
            continue
        if created < cutoff:
            out.append(
                PruneCandidate(
                    section="notes",
                    item_id=note.id,
                    reason="stale_observation",
                    summary=(
                        f"note {note.id} is an unconfirmed observation older "
                        f"than 90 days ({note.metadata.created})"
                    ),
                )
            )
    return out


def _low_confidence_patterns(memory: MemoryFile, now: datetime) -> list[PruneCandidate]:
    cutoff = now - PATTERN_MAX_AGE
    out: list[PruneCandidate] = []
    for pattern in memory.patterns:
        if getattr(pattern, "priority", None) == "critical":
            continue
        if pattern.confidence >= LOW_CONFIDENCE_THRESHOLD:
            continue
        first = _parse_iso(pattern.first_observed)
        if first is None or first >= cutoff:
            continue
        out.append(
            PruneCandidate(
                section="patterns",
                item_id=pattern.id,
                reason="low_confidence_pattern",
                summary=(
                    f"pattern {pattern.id} confidence={pattern.confidence:.2f} "
                    f"first_observed={pattern.first_observed}"
                ),
            )
        )
    return out


def _stale_patterns(memory: MemoryFile, now: datetime) -> list[PruneCandidate]:
    """Behavioral patterns not re-confirmed within the staleness window.

    Only auto-expires machine-detected (``source == "behavioral"``)
    patterns — user/observation-derived ones are left alone. Confidence
    is irrelevant here: the drifted-id orphans that bloat the file are
    high-confidence, and a live behavior gets re-confirmed nightly.
    """
    cutoff = now - PATTERN_STALE_MAX_AGE
    out: list[PruneCandidate] = []
    for pattern in memory.patterns:
        if getattr(pattern, "priority", None) == "critical":
            continue
        if pattern.source != "behavioral":
            continue
        last = _parse_iso(pattern.last_confirmed) or _parse_iso(pattern.first_observed)
        if last is None or last >= cutoff:
            continue
        out.append(
            PruneCandidate(
                section="patterns",
                item_id=pattern.id,
                reason="stale_pattern",
                summary=(
                    f"pattern {pattern.id} not confirmed since "
                    f"{pattern.last_confirmed or pattern.first_observed}"
                ),
            )
        )
    return out


def _excess_patterns(
    memory: MemoryFile,
    *,
    already_flagged: set[str],
) -> list[PruneCandidate]:
    """Cap total patterns at ``MAX_PATTERNS``, keeping the strongest.

    Counts only what would remain after the other pattern rules drop
    their candidates. Critical and non-behavioral patterns are always
    kept; the rest are ranked by (confidence, last_confirmed) and the
    overflow is flagged.
    """
    remaining = [p for p in memory.patterns if p.id not in already_flagged]
    if len(remaining) <= MAX_PATTERNS:
        return []

    def is_protected(p: object) -> bool:
        return (
            getattr(p, "priority", None) == "critical" or getattr(p, "source", "") != "behavioral"
        )

    protected = [p for p in remaining if is_protected(p)]
    prunable = [p for p in remaining if not is_protected(p)]

    # Protected patterns are never force-dropped, so the stored total can
    # exceed MAX_PATTERNS if they alone do — acceptable, since the bloat
    # this guards against is always machine-generated (behavioral).

    keep = max(0, MAX_PATTERNS - len(protected))
    _epoch = datetime.min.replace(tzinfo=UTC)
    prunable.sort(
        key=lambda p: (p.confidence, _parse_iso(p.last_confirmed) or _epoch),
        reverse=True,
    )
    return [
        PruneCandidate(
            section="patterns",
            item_id=p.id,
            reason="pattern_cap_exceeded",
            summary=f"pattern {p.id} over cap of {MAX_PATTERNS} (confidence={p.confidence:.2f})",
        )
        for p in prunable[keep:]
    ]


def _old_rejections(memory: MemoryFile, now: datetime) -> list[PruneCandidate]:
    cutoff = now - REJECTED_MAX_AGE
    out: list[PruneCandidate] = []
    for rejection in memory.rejected:
        date = _parse_iso(rejection.date)
        if date is None or date >= cutoff:
            continue
        out.append(
            PruneCandidate(
                section="rejected",
                item_id=rejection.id,
                reason="old_rejection",
                summary=f"rejection {rejection.id} from {rejection.date}",
            )
        )
    return out


def _rank_low_reference_notes(
    memory: MemoryFile,
    now: datetime,
    *,
    exclude: set[tuple[str, str]],
) -> list[PruneCandidate]:
    """Sort non-protected notes by (reference_count asc, age desc).

    The combined score is ``reference_count + days_since_last_reference /
    365`` — a rough "importance" that favors frequently-used notes over
    rarely-used ones without letting pure recency dominate. Lower score
    is a better prune candidate.
    """
    scored: list[tuple[float, Note]] = []
    for note in memory.notes:
        if _is_protected_note(note):
            continue
        if ("notes", note.id) in exclude:
            continue
        last = _parse_iso(note.metadata.last_referenced) or _parse_iso(note.metadata.created)
        age_days = (now - last).days if last else 0
        score = note.metadata.reference_count + (age_days / -365.0)
        scored.append((score, note))

    scored.sort(key=lambda pair: pair[0])  # lowest score first

    out: list[PruneCandidate] = []
    for _score, note in scored:
        out.append(
            PruneCandidate(
                section="notes",
                item_id=note.id,
                reason="low_reference",
                summary=(
                    f"note {note.id} ref_count={note.metadata.reference_count} "
                    f"last_ref={note.metadata.last_referenced or '—'}"
                ),
            )
        )
    return out
