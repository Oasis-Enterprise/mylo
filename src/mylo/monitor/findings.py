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

"""Findings store — the bounded, self-cleaning replacement for the
old append-forever pending_actions semantics.

Findings still live in ``MemoryFile.pending_actions`` (same field,
same catch-up banner plumbing) but with new lifecycle rules:

* Keyed by ``(type, entity_id)`` — re-detection refreshes, never duplicates.
* Auto-resolve — a successful sweep deletes findings it no longer sees.
* TTL — findings older than 48h are deleted regardless.
* Cap — at most 5 active findings; lowest confidence evicted.
* Dismiss — sets a 7-day per-(type, entity) cooldown, then deletes.

Legacy entries (written before ``last_seen`` existed) are dropped by
``migrate_legacy`` on the first sweep after upgrade.

**Important invariants for callers:**

* All ``now`` arguments must be timezone-aware UTC datetimes.  ISO-string
  comparisons in this module depend on a uniform ``+00:00`` suffix; naive
  datetimes will silently produce wrong ordering.

* Intended per-sweep call order::

      migrate_legacy(memory)                         # once, on startup
      for each detection type:
          upsert_finding(...)   # 0-N times
          resolve_stale(...)    # once after successful check
      expire_old(memory, now)                        # last, every sweep
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mylo.logging_setup import get_logger
from mylo.memory.schema import FindingCooldown, MemoryFile, PendingAction

log = get_logger(__name__)

MAX_ACTIVE_FINDINGS = 5
FINDING_TTL_HOURS = 48
DISMISS_COOLDOWN_DAYS = 7


def upsert_finding(
    memory: MemoryFile,
    *,
    finding_id: str,
    finding_type: str,
    entity_id: str,
    title: str,
    message: str,
    confidence: float,
    now: datetime,
) -> bool:
    """Insert or refresh a finding.

    Returns True only when newly inserted AND the finding survives the cap
    eviction (i.e. it is still present in ``memory.pending_actions`` after
    the loop).  Returns False on a refresh of an existing finding.
    Cooled-down ``(type, entity_id)`` pairs are rejected silently — callers
    are expected to check ``in_cooldown`` first, but this guard makes the
    invariant unbreakable across all call sites.
    """
    # Defensive guard: never insert a cooled-down pair regardless of caller.
    if in_cooldown(memory, finding_type, entity_id, now):
        return False

    # User suppressions ("ignore this entity" via
    # manage_notification_filters) are enforced HERE — the single choke
    # point every producer flows through. A suppressed pair neither
    # inserts nor refreshes.
    if memory.is_notification_suppressed(finding_type, entity_id or None):
        return False

    ts = now.isoformat(timespec="seconds")
    for pa in memory.pending_actions:
        if pa.type == finding_type and pa.entity_id == entity_id:
            pa.title = title
            pa.message = message
            pa.last_seen = ts
            pa.confidence = confidence
            return False

    new_finding = PendingAction(
        id=finding_id,
        type=finding_type,
        entity_id=entity_id,
        title=title,
        message=message,
        detected_at=ts,
        last_seen=ts,
        confidence=confidence,
    )
    memory.pending_actions.append(new_finding)
    while len(memory.pending_actions) > MAX_ACTIVE_FINDINGS:
        lowest = min(memory.pending_actions, key=lambda pa: pa.confidence)
        log.debug(
            "findings.evicted",
            type=lowest.type,
            entity_id=lowest.entity_id,
            confidence=lowest.confidence,
        )
        memory.pending_actions.remove(lowest)
    # Return True only if the new finding survived eviction.
    return new_finding in memory.pending_actions


def resolve_stale(memory: MemoryFile, finding_type: str, active_entity_ids: set[str]) -> int:
    """Delete findings of this type that the sweep no longer sees.

    Call only after a SUCCESSFUL check of that type — a failed sweep
    must not resolve anything. Returns the number removed.
    """
    before = len(memory.pending_actions)
    memory.pending_actions = [
        pa
        for pa in memory.pending_actions
        if pa.type != finding_type or pa.entity_id in active_entity_ids
    ]
    return before - len(memory.pending_actions)


def expire_old(memory: MemoryFile, now: datetime) -> int:
    """Delete findings older than the TTL and prune expired cooldowns.

    Returns the number of findings removed (cooldown pruning is side-effect
    only and not reflected in the return value).
    """
    cutoff = (now - timedelta(hours=FINDING_TTL_HOURS)).isoformat(timespec="seconds")
    before = len(memory.pending_actions)
    memory.pending_actions = [pa for pa in memory.pending_actions if pa.detected_at >= cutoff]
    # Prune expired cooldowns — compare against NOW, not the 48h finding cutoff.
    cutoff_ts = now.isoformat(timespec="seconds")
    memory.finding_cooldowns = [cd for cd in memory.finding_cooldowns if cd.until > cutoff_ts]
    return before - len(memory.pending_actions)


def in_cooldown(memory: MemoryFile, finding_type: str, entity_id: str, now: datetime) -> bool:
    ts = now.isoformat(timespec="seconds")
    return any(
        cd.type == finding_type and cd.entity_id == entity_id and cd.until > ts
        for cd in memory.finding_cooldowns
    )


def _set_cooldown(memory: MemoryFile, finding_type: str, entity_id: str, now: datetime) -> None:
    ts = now.isoformat(timespec="seconds")
    until = (now + timedelta(days=DISMISS_COOLDOWN_DAYS)).isoformat(timespec="seconds")
    # Prune expired cooldowns while we're here — keeps the list bounded.
    memory.finding_cooldowns = [cd for cd in memory.finding_cooldowns if cd.until > ts]
    for cd in memory.finding_cooldowns:
        if cd.type == finding_type and cd.entity_id == entity_id:
            cd.until = until
            return
    memory.finding_cooldowns.append(
        FindingCooldown(type=finding_type, entity_id=entity_id, until=until)
    )


def dismiss_finding(memory: MemoryFile, finding_id: str, now: datetime) -> bool:
    """Dismiss one finding: cooldown its (type, entity), then delete."""
    for pa in memory.pending_actions:
        if pa.id == finding_id:
            _set_cooldown(memory, pa.type, pa.entity_id, now)
            memory.pending_actions.remove(pa)
            return True
    return False


def dismiss_all(memory: MemoryFile, now: datetime) -> int:
    """Dismiss everything currently in the store."""
    count = len(memory.pending_actions)
    for pa in memory.pending_actions:
        _set_cooldown(memory, pa.type, pa.entity_id, now)
    memory.pending_actions = []
    return count


def migrate_legacy(memory: MemoryFile) -> int:
    """Drop pre-rework entries: anything missing ``last_seen``.

    Legacy entries never have ``last_seen`` set, so that field alone
    identifies them.  We intentionally do NOT also filter on ``resolved``
    because doing so would turn migration into a cooldown-bypassing delete
    once anything sets ``resolved`` on new-style findings.
    """
    before = len(memory.pending_actions)
    memory.pending_actions = [pa for pa in memory.pending_actions if pa.last_seen is not None]
    dropped = before - len(memory.pending_actions)
    if dropped:
        log.info("findings.migrated_legacy", dropped=dropped)
    return dropped
