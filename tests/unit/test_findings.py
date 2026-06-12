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

"""Tests for the bounded findings store that replaces append-forever
pending_actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mylo.memory.schema import PendingAction, empty_memory
from mylo.monitor.findings import (
    dismiss_all,
    dismiss_finding,
    expire_old,
    in_cooldown,
    migrate_legacy,
    resolve_stale,
    upsert_finding,
)

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)


def _upsert(memory, entity_id="light.kitchen", ftype="duration_anomaly", conf=0.9, now=NOW):
    return upsert_finding(
        memory,
        finding_id=f"{ftype}_{entity_id}",
        finding_type=ftype,
        entity_id=entity_id,
        title="t",
        message="m",
        confidence=conf,
        now=now,
    )


def test_upsert_is_keyed_not_appended() -> None:
    memory = empty_memory()
    assert _upsert(memory) is True
    assert _upsert(memory, now=NOW + timedelta(hours=1)) is False
    assert len(memory.pending_actions) == 1
    assert memory.pending_actions[0].last_seen == (NOW + timedelta(hours=1)).isoformat(
        timespec="seconds"
    )


def test_cap_evicts_lowest_confidence() -> None:
    memory = empty_memory()
    for i in range(5):
        _upsert(memory, entity_id=f"light.l{i}", conf=0.5 + i / 10)
    _upsert(memory, entity_id="light.high", conf=0.99)
    assert len(memory.pending_actions) == 5
    entity_ids = {pa.entity_id for pa in memory.pending_actions}
    assert "light.l0" not in entity_ids  # lowest confidence evicted
    assert "light.high" in entity_ids


def test_resolve_stale_deletes_cleared_findings() -> None:
    memory = empty_memory()
    _upsert(memory, entity_id="light.a")
    _upsert(memory, entity_id="light.b")
    removed = resolve_stale(memory, "duration_anomaly", {"light.b"})
    assert removed == 1
    assert [pa.entity_id for pa in memory.pending_actions] == ["light.b"]


def test_resolve_stale_only_touches_its_type() -> None:
    memory = empty_memory()
    _upsert(memory, entity_id="light.a", ftype="duration_anomaly")
    _upsert(memory, entity_id="light.a", ftype="while_away")
    resolve_stale(memory, "duration_anomaly", set())
    assert len(memory.pending_actions) == 1
    assert memory.pending_actions[0].type == "while_away"


def test_expire_old_uses_48h_ttl() -> None:
    memory = empty_memory()
    _upsert(memory, entity_id="light.old", now=NOW - timedelta(hours=49))
    _upsert(memory, entity_id="light.new", now=NOW - timedelta(hours=1))
    removed = expire_old(memory, NOW)
    assert removed == 1
    assert memory.pending_actions[0].entity_id == "light.new"


def test_dismiss_sets_cooldown_and_deletes() -> None:
    memory = empty_memory()
    _upsert(memory)
    assert dismiss_finding(memory, "duration_anomaly_light.kitchen", NOW) is True
    assert memory.pending_actions == []
    assert in_cooldown(memory, "duration_anomaly", "light.kitchen", NOW) is True
    assert (
        in_cooldown(memory, "duration_anomaly", "light.kitchen", NOW + timedelta(days=8)) is False
    )
    assert in_cooldown(memory, "while_away", "light.kitchen", NOW) is False


def test_dismiss_all_cools_everything() -> None:
    memory = empty_memory()
    _upsert(memory, entity_id="light.a")
    _upsert(memory, entity_id="light.b", ftype="while_away")
    assert dismiss_all(memory, NOW) == 2
    assert memory.pending_actions == []
    assert in_cooldown(memory, "duration_anomaly", "light.a", NOW)
    assert in_cooldown(memory, "while_away", "light.b", NOW)


def test_migrate_legacy_drops_old_entries() -> None:
    memory = empty_memory()
    # Legacy entry: written before last_seen existed.
    memory.pending_actions.append(
        PendingAction(
            id="on_while_away_light.x",
            type="on_while_away",
            entity_id="light.x",
            title="t",
            message="m",
            detected_at="2026-05-01T00:00:00+00:00",
        )
    )
    _upsert(memory)  # new-style entry survives
    assert migrate_legacy(memory) == 1
    assert len(memory.pending_actions) == 1
    assert memory.pending_actions[0].type == "duration_anomaly"


def test_cap_lowest_confidence_not_inserted() -> None:
    """Upsert at cap with lowest confidence: finding is evicted, returns False."""
    memory = empty_memory()
    for i in range(5):
        _upsert(memory, entity_id=f"light.l{i}", conf=0.5 + i / 10)
    result = _upsert(memory, entity_id="light.low", conf=0.1)
    assert result is False
    assert len(memory.pending_actions) == 5
    entity_ids = {pa.entity_id for pa in memory.pending_actions}
    assert "light.low" not in entity_ids


def test_migrate_legacy_idempotent() -> None:
    """Second call to migrate_legacy on a clean store drops nothing."""
    memory = empty_memory()
    memory.pending_actions.append(
        PendingAction(
            id="on_while_away_light.x",
            type="on_while_away",
            entity_id="light.x",
            title="t",
            message="m",
            detected_at="2026-05-01T00:00:00+00:00",
        )
    )
    assert migrate_legacy(memory) == 1
    assert migrate_legacy(memory) == 0


def test_dismiss_finding_unknown_id_returns_false() -> None:
    """Dismissing a non-existent id returns False and leaves store unchanged."""
    memory = empty_memory()
    _upsert(memory)
    result = dismiss_finding(memory, "nonexistent_id", NOW)
    assert result is False
    assert len(memory.pending_actions) == 1


def test_expire_old_prunes_cooldowns() -> None:
    """expire_old called 8 days after dismiss clears the cooldown."""
    memory = empty_memory()
    _upsert(memory)
    dismiss_finding(memory, "duration_anomaly_light.kitchen", NOW)
    # Cooldown should exist now.
    assert len(memory.finding_cooldowns) == 1
    # Advance time past the 7-day cooldown window.
    expire_old(memory, NOW + timedelta(days=8))
    assert memory.finding_cooldowns == []


def test_upsert_refresh_updates_title_and_message() -> None:
    """Refreshing an existing finding updates title and message, not just last_seen."""
    memory = empty_memory()
    _upsert(memory, entity_id="light.kitchen")
    upsert_finding(
        memory,
        finding_id="duration_anomaly_light.kitchen",
        finding_type="duration_anomaly",
        entity_id="light.kitchen",
        title="new title",
        message="new message",
        confidence=0.9,
        now=NOW + timedelta(hours=1),
    )
    assert len(memory.pending_actions) == 1
    pa = memory.pending_actions[0]
    assert pa.title == "new title"
    assert pa.message == "new message"


def test_upsert_while_in_cooldown_returns_false() -> None:
    """Upsert of a dismissed (cooled-down) pair returns False and does not insert."""
    memory = empty_memory()
    _upsert(memory)
    dismiss_finding(memory, "duration_anomaly_light.kitchen", NOW)
    assert len(memory.pending_actions) == 0
    result = _upsert(memory)
    assert result is False
    assert len(memory.pending_actions) == 0
