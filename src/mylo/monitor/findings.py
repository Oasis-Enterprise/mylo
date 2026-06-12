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
    """Insert or refresh a finding. Returns True only when newly inserted."""
    ts = now.isoformat(timespec="seconds")
    for pa in memory.pending_actions:
        if pa.type == finding_type and pa.entity_id == entity_id:
            pa.title = title
            pa.message = message
            pa.last_seen = ts
            pa.confidence = confidence
            return False

    memory.pending_actions.append(
        PendingAction(
            id=finding_id,
            type=finding_type,
            entity_id=entity_id,
            title=title,
            message=message,
            detected_at=ts,
            last_seen=ts,
            confidence=confidence,
        )
    )
    while len(memory.pending_actions) > MAX_ACTIVE_FINDINGS:
        lowest = min(memory.pending_actions, key=lambda pa: pa.confidence)
        memory.pending_actions.remove(lowest)
    return True


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
    """Delete findings older than the TTL. Returns the number removed."""
    cutoff = (now - timedelta(hours=FINDING_TTL_HOURS)).isoformat(timespec="seconds")
    before = len(memory.pending_actions)
    memory.pending_actions = [pa for pa in memory.pending_actions if pa.detected_at >= cutoff]
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
    """Drop pre-rework entries: anything resolved or missing last_seen."""
    before = len(memory.pending_actions)
    memory.pending_actions = [
        pa for pa in memory.pending_actions if pa.last_seen is not None and not pa.resolved
    ]
    dropped = before - len(memory.pending_actions)
    if dropped:
        log.info("findings.migrated_legacy", dropped=dropped)
    return dropped
