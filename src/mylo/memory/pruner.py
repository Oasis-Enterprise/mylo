"""Deterministic ranked memory pruning (spec §3.4).

The reconciler's LLM merge can add items, but it cannot drop them —
that's this module's job. Pruning is rule-based on purpose: the user
must trust that nothing disappears for mysterious LLM reasons.

Priority order (first pruned first):

1. Expired TTL items
2. Observations never confirmed, older than 90 days
3. Lowest reference_count + oldest last_referenced
4. Resolved known_issues (archive, not delete)
5. Patterns with confidence < 0.5, older than 60 days
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

PruneReason = Literal[
    "ttl_expired",
    "stale_observation",
    "low_reference",
    "resolved_issue",
    "low_confidence_pattern",
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

    # 5. Low-confidence old patterns.
    candidates.extend(_low_confidence_patterns(memory, current))

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
