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

"""Tests for M8b: pruner, reconciler, and /api/memory/sync endpoint.

The reconciler is LLM-driven in production; here we stub the provider
so the test asserts the orchestration (YAML parse → protect → prune
plan) without a real API call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from mylo.memory.pruner import apply_prune, plan_prune
from mylo.memory.reconciler import StateDiff, _build_user_payload, run_sync
from mylo.memory.schema import (
    Baselines,
    Claim,
    Conflict,
    EntityBaseline,
    FindingCooldown,
    HouseholdMember,
    ItemMetadata,
    KnownIssue,
    Note,
    NotificationSuppression,
    Pattern,
    PendingAction,
    RejectedSuggestion,
    Suggestion,
    empty_memory,
)
from mylo.memory.store import MemoryStore
from mylo.server.routes_memory import register_memory_routes

NOW = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)


def iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# ─── Pruner ─────────────────────────────────────────────────────────────────


def test_pruner_drops_expired_ttl() -> None:
    mem = empty_memory()
    mem.notes.append(
        Note(
            id="n_expired",
            content="ephemeral",
            metadata=ItemMetadata(
                created=iso(30),
                ttl=iso(1),  # TTL was yesterday
            ),
        )
    )
    mem.notes.append(
        Note(
            id="n_live",
            content="still relevant",
            metadata=ItemMetadata(
                created=iso(30),
                ttl=(NOW + timedelta(days=30)).isoformat(),
            ),
        )
    )
    report = plan_prune(mem, now=NOW)
    reasons = {c.item_id: c.reason for c in report.candidates}
    assert reasons.get("n_expired") == "ttl_expired"
    assert "n_live" not in reasons


def test_pruner_flags_stale_unconfirmed_observations() -> None:
    mem = empty_memory()
    mem.notes.append(
        Note(
            id="n_stale",
            content="seen once, never confirmed",
            source="observation",
            metadata=ItemMetadata(created=iso(120), source="observation"),
        )
    )
    mem.notes.append(
        Note(
            id="n_fresh",
            content="seen yesterday",
            source="observation",
            metadata=ItemMetadata(created=iso(10), source="observation"),
        )
    )
    report = plan_prune(mem, now=NOW)
    reasons = {c.item_id: c.reason for c in report.candidates}
    assert reasons.get("n_stale") == "stale_observation"
    assert "n_fresh" not in reasons


def test_pruner_never_drops_user_confirmed_or_critical() -> None:
    mem = empty_memory()
    mem.notes.append(
        Note(
            id="n_user",
            content="user confirmed this",
            source="user_confirmed",
            metadata=ItemMetadata(
                created=iso(400),
                source="user_confirmed",
                ttl=iso(30),  # would normally expire
            ),
        )
    )
    mem.notes.append(
        Note(
            id="n_crit",
            content="critical note",
            source="conversation",
            metadata=ItemMetadata(
                created=iso(400),
                priority="critical",
                ttl=iso(30),  # would normally expire
            ),
        )
    )
    report = plan_prune(mem, now=NOW)
    ids = {c.item_id for c in report.candidates}
    assert "n_user" not in ids
    assert "n_crit" not in ids


def test_pruner_archives_resolved_issues() -> None:
    mem = empty_memory()
    mem.known_issues.append(KnownIssue(id="i_resolved", description="fixed", status="resolved"))
    mem.known_issues.append(KnownIssue(id="i_active", description="still broken", status="active"))
    report = plan_prune(mem, now=NOW)
    reasons = {c.item_id: c.reason for c in report.candidates}
    assert reasons.get("i_resolved") == "resolved_issue"
    assert "i_active" not in reasons


def test_pruner_drops_low_confidence_old_patterns() -> None:
    mem = empty_memory()
    mem.patterns.append(
        Pattern(
            id="p_lowold",
            description="weak old pattern",
            confidence=0.3,
            first_observed=iso(90),
        )
    )
    mem.patterns.append(
        Pattern(
            id="p_highold",
            description="confident old pattern",
            confidence=0.9,
            first_observed=iso(90),
        )
    )
    mem.patterns.append(
        Pattern(
            id="p_lowfresh",
            description="weak but recent",
            confidence=0.3,
            first_observed=iso(20),
        )
    )
    report = plan_prune(mem, now=NOW)
    ids = {c.item_id for c in report.candidates}
    assert "p_lowold" in ids
    assert "p_highold" not in ids
    assert "p_lowfresh" not in ids


def test_pruner_drops_old_rejections() -> None:
    mem = empty_memory()
    mem.rejected.append(RejectedSuggestion(id="r_old", suggestion="no thanks", date=iso(200)))
    mem.rejected.append(RejectedSuggestion(id="r_new", suggestion="still relevant", date=iso(30)))
    report = plan_prune(mem, now=NOW)
    ids = {c.item_id for c in report.candidates}
    assert "r_old" in ids
    assert "r_new" not in ids


def test_pruner_low_reference_respects_budget() -> None:
    mem = empty_memory()
    for i in range(10):
        mem.notes.append(
            Note(
                id=f"n_{i}",
                content=f"note {i}",
                metadata=ItemMetadata(
                    created=iso(60 + i),
                    reference_count=i,  # 0..9
                ),
            )
        )
    report = plan_prune(mem, now=NOW, target_budget=3)
    low_ref = [c for c in report.candidates if c.reason == "low_reference"]
    assert len(low_ref) == 3
    # Lowest ref_count should be flagged first.
    assert low_ref[0].item_id == "n_0"


def test_apply_prune_removes_flagged_items() -> None:
    mem = empty_memory()
    mem.notes.append(
        Note(
            id="n_drop",
            content="bye",
            metadata=ItemMetadata(created=iso(30), ttl=iso(1)),
        )
    )
    mem.notes.append(Note(id="n_keep", content="stays"))
    report = plan_prune(mem, now=NOW)
    pruned = apply_prune(mem, report)
    ids = {n.id for n in pruned.notes}
    assert ids == {"n_keep"}


# ─── Reconciler (with stubbed provider) ─────────────────────────────────────


@dataclass
class _FakeResponse:
    text: str
    content_blocks: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)


class _FakeProvider:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def message(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(text=self.reply)


async def test_reconciler_noop_without_scratchpad_or_diff(tmp_path: Path) -> None:
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()
    provider = _FakeProvider(reply="version: 2\n")  # should not be called

    result = await run_sync(
        store=store,
        provider=provider,
        registries=None,
        model="claude-haiku-4-5-20251001",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    assert result.updated is None
    assert "no new scratchpad" in result.summary
    assert provider.calls == []


async def test_reconciler_parses_yaml_and_preserves_user_sections(tmp_path: Path) -> None:
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()

    # Seed memory with household + user-confirmed note.
    mem = empty_memory()
    mem.household.members.append(HouseholdMember(name="Maxwell", role="primary_user"))
    mem.notes.append(
        Note(
            id="n_user",
            content="garage light off after 8pm",
            source="user_confirmed",
            metadata=ItemMetadata(created=iso(30), source="user_confirmed"),
        )
    )
    await store.save(mem, note="seed")

    # Scratchpad entry so reconciler actually runs.
    (tmp_path / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, '
        'content: "likes warm light", recorded: "2026-04-12", '
        'confidence: 0.9, conversation_id: "c1"}\n'
    )

    # Haiku-style reply — drops household AND user-confirmed note on purpose.
    # The reconciler should re-add them via _protect_user_sections.
    model_reply = (
        "version: 2\n"
        "last_sync: '2026-04-13T10:00:00+00:00'\n"
        "household:\n"
        "  members: []\n"
        "  shared: {}\n"
        "notes:\n"
        "  - id: n_new\n"
        "    content: likes warm light\n"
        "    metadata:\n"
        "      created: '2026-04-13T10:00:00+00:00'\n"
        "      source: conversation\n"
        "      reference_count: 1\n"
        "known_issues: []\n"
        "patterns: []\n"
        "rejected: []\n"
        "conflicts: []\n"
        "monitored_entities: []\n"
    )
    provider = _FakeProvider(reply=model_reply)

    result = await run_sync(
        store=store,
        provider=provider,
        registries=None,
        model="haiku",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    assert result.updated is not None
    # Household restored from original.
    assert [m.name for m in result.updated.household.members] == ["Maxwell"]
    # User-confirmed note re-added.
    ids = {n.id for n in result.updated.notes}
    assert "n_user" in ids
    assert "n_new" in ids


async def test_reconciler_handles_malformed_yaml(tmp_path: Path) -> None:
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()
    (tmp_path / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, '
        'content: "anything", recorded: "2026-04-12", '
        'confidence: 1.0, conversation_id: "c1"}\n'
    )
    provider = _FakeProvider(reply=":::not valid yaml::: [[[")

    result = await run_sync(
        store=store,
        provider=provider,
        registries=None,
        model="haiku",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    assert result.updated is None
    assert "malformed" in result.summary


async def test_reconciler_emits_conflict_on_contradiction(tmp_path: Path) -> None:
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()
    (tmp_path / "scratchpad.yaml").write_text(
        '- {type: "observation", scope: {entity: "cover.garage_door"}, '
        'content: "garage door fails 4x/month", recorded: "2026-04-12", '
        'confidence: 0.9, conversation_id: "c1"}\n'
    )

    # Reply adds a conflict where the scratchpad contradicts existing memory.
    model_reply = (
        "version: 2\n"
        "last_sync: '2026-04-13T10:00:00+00:00'\n"
        "household:\n"
        "  members: []\n"
        "  shared: {}\n"
        "notes: []\n"
        "known_issues: []\n"
        "patterns: []\n"
        "rejected: []\n"
        "monitored_entities: []\n"
        "conflicts:\n"
        "  - id: conflict_abc123\n"
        "    type: contradiction\n"
        "    subject: {entity: 'cover.garage_door'}\n"
        "    claim_a:\n"
        "      content: 'garage door works fine'\n"
        "      source: memory\n"
        "      date: '2026-03-01'\n"
        "    claim_b:\n"
        "      content: 'garage door fails 4x/month'\n"
        "      source: scratchpad\n"
        "      date: '2026-04-12'\n"
        "    status: pending_review\n"
    )
    provider = _FakeProvider(reply=model_reply)

    result = await run_sync(
        store=store,
        provider=provider,
        registries=None,
        model="haiku",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    assert result.updated is not None
    assert result.conflicts_added == 1
    assert len(result.updated.pending_conflicts()) == 1


async def test_reconciler_fallback_without_provider(tmp_path: Path) -> None:
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()
    (tmp_path / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, '
        'content: "offline-merged note", recorded: "2026-04-12", '
        'confidence: 1.0, conversation_id: "c1"}\n'
    )

    result = await run_sync(
        store=store,
        provider=None,
        registries=None,
        model="haiku",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    assert result.updated is not None
    contents = [n.content for n in result.updated.notes]
    assert "offline-merged note" in contents


# ─── Endpoint smoke ─────────────────────────────────────────────────────────


@pytest.fixture
def memory_app(tmp_path: Path) -> web.Application:
    from mylo.server.app import AppKeys
    from tests.unit._helpers import make_config

    app = web.Application()
    config = make_config(tmp_path)
    config.mylo_data_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(mylo_data_dir=config.mylo_data_dir)

    app[AppKeys.CONFIG] = config
    app[AppKeys.MEMORY] = store
    register_memory_routes(app)
    return app


async def test_get_memory_endpoint(aiohttp_client, memory_app: web.Application) -> None:
    from mylo.server.app import AppKeys

    store = memory_app[AppKeys.MEMORY]
    await store.load()

    client = await aiohttp_client(memory_app)
    resp = await client.get("/api/memory")
    assert resp.status == 200
    body = await resp.json()
    assert body["version"] == 2
    assert body["counts"]["notes"] == 0


async def test_sync_endpoint_without_provider_is_graceful(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    # No AppKeys.PROVIDER set → endpoint should still return 200.
    config = memory_app[AppKeys.CONFIG]
    (config.mylo_data_dir / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, '
        'content: "endpoint test note", recorded: "2026-04-12", '
        'confidence: 1.0, conversation_id: "c1"}\n'
    )
    store = memory_app[AppKeys.MEMORY]
    await store.load()

    client = await aiohttp_client(memory_app)
    resp = await client.post("/api/memory/sync", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["changed"] is True  # fallback merge landed the scratchpad note


async def test_sync_endpoint_apply_prune_flag(aiohttp_client, memory_app: web.Application) -> None:
    from mylo.server.app import AppKeys

    store = memory_app[AppKeys.MEMORY]
    mem = await store.load()
    mem.notes.append(
        Note(
            id="n_expired",
            content="stale",
            metadata=ItemMetadata(created=iso(30), ttl=iso(1)),
        )
    )
    await store.save(mem, note="seed")

    client = await aiohttp_client(memory_app)
    resp = await client.post("/api/memory/sync", json={"apply_prune": True})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["applied"] is True
    assert any(c["id"] == "n_expired" for c in body["prune_candidates"])

    # Reload and confirm the stale note is gone on disk.
    reloaded = await MemoryStore(mylo_data_dir=store.mylo_data_dir).load()
    assert all(n.id != "n_expired" for n in reloaded.notes)


# ─── M8c: full view, delete, prune-only, conflict resolve ───────────────────


async def test_get_memory_full_returns_whole_document(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    store = memory_app[AppKeys.MEMORY]
    mem = await store.load()
    mem.notes.append(Note(id="n1", content="keep me"))
    mem.household.members.append(HouseholdMember(name="Alice", role="primary_user"))
    await store.save(mem, note="seed")

    client = await aiohttp_client(memory_app)
    resp = await client.get("/api/memory/full")
    assert resp.status == 200
    body = await resp.json()
    assert body["version"] == 2
    assert [n["id"] for n in body["notes"]] == ["n1"]
    assert [m["name"] for m in body["household"]["members"]] == ["Alice"]


async def test_delete_memory_item_removes_note(aiohttp_client, memory_app: web.Application) -> None:
    from mylo.server.app import AppKeys

    store = memory_app[AppKeys.MEMORY]
    mem = await store.load()
    mem.notes.append(Note(id="n_drop", content="bye"))
    mem.notes.append(Note(id="n_keep", content="stays"))
    await store.save(mem, note="seed")

    client = await aiohttp_client(memory_app)
    resp = await client.delete("/api/memory/item", json={"section": "notes", "id": "n_drop"})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True

    reloaded = await MemoryStore(mylo_data_dir=store.mylo_data_dir).load()
    assert {n.id for n in reloaded.notes} == {"n_keep"}


async def test_delete_memory_item_rejects_unknown_section(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    await memory_app[AppKeys.MEMORY].load()
    client = await aiohttp_client(memory_app)
    resp = await client.delete("/api/memory/item", json={"section": "household", "id": "anyone"})
    assert resp.status == 400


async def test_delete_memory_item_404_when_id_missing(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    await memory_app[AppKeys.MEMORY].load()
    client = await aiohttp_client(memory_app)
    resp = await client.delete(
        "/api/memory/item", json={"section": "notes", "id": "does_not_exist"}
    )
    assert resp.status == 404


async def test_prune_only_endpoint_drops_expired(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    store = memory_app[AppKeys.MEMORY]
    mem = await store.load()
    mem.notes.append(
        Note(
            id="n_expired",
            content="stale",
            metadata=ItemMetadata(created=iso(30), ttl=iso(1)),
        )
    )
    mem.notes.append(Note(id="n_keep", content="stays"))
    await store.save(mem, note="seed")

    client = await aiohttp_client(memory_app)
    resp = await client.post("/api/memory/prune", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["applied"] == 1

    reloaded = await MemoryStore(mylo_data_dir=store.mylo_data_dir).load()
    assert {n.id for n in reloaded.notes} == {"n_keep"}


async def test_prune_only_honors_ids_filter(aiohttp_client, memory_app: web.Application) -> None:
    from mylo.server.app import AppKeys

    store = memory_app[AppKeys.MEMORY]
    mem = await store.load()
    mem.notes.append(
        Note(
            id="n_a",
            content="stale a",
            metadata=ItemMetadata(created=iso(30), ttl=iso(1)),
        )
    )
    mem.notes.append(
        Note(
            id="n_b",
            content="stale b",
            metadata=ItemMetadata(created=iso(30), ttl=iso(1)),
        )
    )
    await store.save(mem, note="seed")

    client = await aiohttp_client(memory_app)
    resp = await client.post("/api/memory/prune", json={"ids": ["n_a"]})
    assert resp.status == 200
    body = await resp.json()
    assert body["applied"] == 1

    reloaded = await MemoryStore(mylo_data_dir=store.mylo_data_dir).load()
    assert {n.id for n in reloaded.notes} == {"n_b"}


async def test_resolve_conflict_marks_status_resolved(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    store = memory_app[AppKeys.MEMORY]
    mem = await store.load()
    mem.conflicts.append(
        Conflict(
            id="conflict_abc",
            type="contradiction",
            subject={"entity": "cover.garage_door"},
            claim_a=Claim(content="works fine", source="memory"),
            claim_b=Claim(content="fails 4x/month", source="scratchpad"),
            status="pending_review",
        )
    )
    await store.save(mem, note="seed")

    client = await aiohttp_client(memory_app)
    resp = await client.post("/api/memory/conflict/conflict_abc/resolve", json={"choice": "b"})
    assert resp.status == 200

    reloaded = await MemoryStore(mylo_data_dir=store.mylo_data_dir).load()
    assert reloaded.conflicts[0].status == "resolved"
    assert reloaded.conflicts[0].resolution is not None
    assert reloaded.conflicts[0].resolution["choice"] == "b"
    assert reloaded.pending_conflicts() == []


async def test_resolve_conflict_404_for_unknown_id(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    await memory_app[AppKeys.MEMORY].load()
    client = await aiohttp_client(memory_app)
    resp = await client.post("/api/memory/conflict/nope/resolve", json={"choice": "a"})
    assert resp.status == 404


async def test_get_scratchpad_endpoint_returns_pending_entries(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    config = memory_app[AppKeys.CONFIG]
    (config.mylo_data_dir / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, '
        'content: "sprinklers offline is fine", recorded: "2026-04-15", '
        'confidence: 1.0, conversation_id: "c1"}\n'
    )
    await memory_app[AppKeys.MEMORY].load()

    client = await aiohttp_client(memory_app)
    resp = await client.get("/api/memory/scratchpad")
    assert resp.status == 200
    body = await resp.json()
    assert len(body["entries"]) == 1
    assert body["entries"][0]["content"] == "sprinklers offline is fine"


async def test_sync_endpoint_drains_scratchpad_on_save(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    config = memory_app[AppKeys.CONFIG]
    scratch = config.mylo_data_dir / "scratchpad.yaml"
    scratch.write_text(
        '- {type: "user_note", scope: {general: true}, '
        'content: "drained after sync", recorded: "2026-04-15", '
        'confidence: 1.0, conversation_id: "c1"}\n'
    )
    await memory_app[AppKeys.MEMORY].load()

    client = await aiohttp_client(memory_app)
    resp = await client.post("/api/memory/sync", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["scratchpad_drained"] == 1
    assert not scratch.exists()
    # Archived to history.
    archived = list((config.mylo_data_dir / "history").glob("scratchpad_*.yaml"))
    assert len(archived) == 1


async def test_get_scratchpad_endpoint_empty_when_missing(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    await memory_app[AppKeys.MEMORY].load()
    client = await aiohttp_client(memory_app)
    resp = await client.get("/api/memory/scratchpad")
    assert resp.status == 200
    body = await resp.json()
    assert body["entries"] == []


# ─── Reconciler prompt bloat (large-instance guards) ─────────────────────────


def test_user_payload_excludes_machine_sections_and_bounds_diff() -> None:
    """The reconciler prompt must not carry machine-owned state, and the
    state diff must be sampled, or large instances blow the context window."""
    mem = empty_memory()
    mem.notes.append(
        Note(id="n1", content="SEMANTIC_NOTE_MARKER", metadata=ItemMetadata(created=iso(1)))
    )
    mem.monitored_entities = [f"sensor.monitored_{i}" for i in range(300)]
    mem.baselines = Baselines(
        entities=[
            EntityBaseline(entity=f"sensor.bl_{i}", metric="mean", avg=1.0, stddev=0.1)
            for i in range(300)
        ]
    )
    mem.pending_actions.append(
        PendingAction(
            id="duration_anomaly_light.x",
            type="duration_anomaly",
            entity_id="light.x",
            title="t",
            message="PENDING_MARKER",
            detected_at=iso(0),
            last_seen=iso(0),
        )
    )
    mem.suggestions.append(
        Suggestion(id="s1", type="while_away", entity_id="light.y", description="SUGGESTION_MARKER")
    )
    mem.finding_cooldowns.append(
        FindingCooldown(type="while_away", entity_id="light.z", until=iso(-7))
    )
    mem.notification_suppressions.append(
        NotificationSuppression(type="anomaly", entity="SUPPRESS_MARKER")
    )

    diff = StateDiff(new_entities=[f"sensor.new_{i}" for i in range(2000)])
    payload = _build_user_payload(mem, [], diff)

    # Semantic data the LLM actually reconciles is present.
    assert "SEMANTIC_NOTE_MARKER" in payload
    # Machine-owned sections are absent.
    assert "sensor.monitored_0" not in payload
    assert "sensor.bl_0" not in payload
    assert "PENDING_MARKER" not in payload
    assert "SUGGESTION_MARKER" not in payload
    assert "SUPPRESS_MARKER" not in payload
    # Diff is sampled, not dumped whole.
    assert "sensor.new_0" in payload
    assert "sensor.new_1999" not in payload
    assert "more" in payload.lower()
    # Payload is small even though the memory was large.
    assert len(payload) < 50_000


async def test_machine_sections_carried_over_untouched(tmp_path: Path) -> None:
    """Machine state must survive a reconcile verbatim — the LLM can neither
    drop it (it's never sent) nor overwrite it (we ignore its version)."""
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()

    mem = empty_memory()
    mem.monitored_entities = ["sensor.keep_me"]
    mem.baselines = Baselines(
        entities=[EntityBaseline(entity="sensor.keep_me", metric="mean", avg=5.0, stddev=1.0)]
    )
    mem.pending_actions.append(
        PendingAction(
            id="duration_anomaly_light.a",
            type="duration_anomaly",
            entity_id="light.a",
            title="t",
            message="m",
            detected_at=iso(0),
            last_seen=iso(0),
        )
    )
    mem.suggestions.append(
        Suggestion(
            id="sg1", type="while_away", entity_id="light.b", description="d", times_accepted=2
        )
    )
    mem.finding_cooldowns.append(
        FindingCooldown(type="while_away", entity_id="light.c", until=iso(-7))
    )
    mem.notification_suppressions.append(NotificationSuppression(type="anomaly", entity="sensor.q"))
    await store.save(mem, note="seed")

    (tmp_path / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, content: "x", '
        'recorded: "2026-04-12", confidence: 0.9, conversation_id: "c1"}\n'
    )

    # Reply omits some machine sections and tries to overwrite others with junk.
    model_reply = (
        "version: 2\n"
        "notes:\n"
        "  - id: n_new\n"
        "    content: x\n"
        "    metadata:\n"
        "      created: '2026-04-13T10:00:00+00:00'\n"
        "      source: conversation\n"
        "      reference_count: 1\n"
        "monitored_entities: ['sensor.LLM_INJECTED']\n"
        "pending_actions: []\n"
        "baselines: {entities: [], energy: null}\n"
        "suggestions: []\n"
    )
    provider = _FakeProvider(reply=model_reply)
    result = await run_sync(
        store=store,
        provider=provider,
        registries=None,
        model="haiku",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    assert result.updated is not None
    m = result.updated
    assert m.monitored_entities == ["sensor.keep_me"]
    assert [b.entity for b in m.baselines.entities] == ["sensor.keep_me"]
    assert [p.id for p in m.pending_actions] == ["duration_anomaly_light.a"]
    assert [s.id for s in m.suggestions] == ["sg1"]
    assert [c.entity_id for c in m.finding_cooldowns] == ["light.c"]
    assert [n.entity for n in m.notification_suppressions] == ["sensor.q"]
    # The model never even saw the machine state.
    sent = provider.calls[0]["messages"][0]["content"]
    assert "sensor.keep_me" not in sent


async def test_oversized_semantic_memory_compacts_and_reconciles(tmp_path: Path) -> None:
    """A memory too big for the window is compacted to fit so the LLM merge
    still runs (instead of being skipped), and the notes dropped from the
    payload are re-attached afterward rather than lost."""
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()

    mem = empty_memory()
    big = "x" * 500
    for i in range(2000):
        mem.notes.append(Note(id=f"n{i}", content=big, metadata=ItemMetadata(created=iso(1))))
    await store.save(mem, note="seed")

    (tmp_path / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, content: "x", '
        'recorded: "2026-04-12", confidence: 0.9, conversation_id: "c1"}\n'
    )
    provider = _FakeProvider(reply="version: 2\n")
    result = await run_sync(
        store=store,
        provider=provider,
        registries=None,
        model="haiku",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    # The LLM was called (no longer skipped) and a merge was produced.
    assert provider.calls != []
    assert result.updated is not None
    assert "too large" not in result.summary
    # The notes compacted out of the payload were re-attached, not lost.
    assert len(result.updated.notes) > 500


# ─── Pattern bloat guards (staleness + cap) ──────────────────────────────────


def test_pruner_drops_stale_behavioral_patterns_regardless_of_confidence() -> None:
    """A behavioral pattern not re-confirmed within the staleness window is
    dead weight even at high confidence (its averaged-time id drifted)."""
    mem = empty_memory()
    mem.patterns.append(
        Pattern(
            id="behavior_light.kitchen_off_2230",
            description="stale but confident",
            confidence=0.9,
            source="behavioral",
            first_observed=iso(40),
            last_confirmed=iso(30),  # > 21d ago
        )
    )
    mem.patterns.append(
        Pattern(
            id="behavior_light.kitchen_off_0700",
            description="fresh",
            confidence=0.9,
            source="behavioral",
            first_observed=iso(40),
            last_confirmed=iso(3),  # recent
        )
    )
    # A user/observation pattern of the same age must NOT be auto-expired.
    mem.patterns.append(
        Pattern(
            id="manual_pattern",
            description="user-derived",
            confidence=0.9,
            source="observation",
            first_observed=iso(40),
            last_confirmed=iso(30),
        )
    )
    report = plan_prune(mem, now=NOW)
    flagged = {c.item_id: c.reason for c in report.candidates}
    assert flagged.get("behavior_light.kitchen_off_2230") == "stale_pattern"
    assert "behavior_light.kitchen_off_0700" not in flagged
    assert "manual_pattern" not in flagged


def test_pruner_caps_pattern_count_keeping_strongest() -> None:
    mem = empty_memory()
    # 250 fresh, high-ish confidence behavioral patterns.
    for i in range(250):
        mem.patterns.append(
            Pattern(
                id=f"behavior_e{i}_off_1200",
                description=f"p{i}",
                confidence=0.5 + (i % 50) / 100.0,  # 0.50..0.99
                source="behavioral",
                first_observed=iso(5),
                last_confirmed=iso(1),  # all fresh — staleness won't fire
            )
        )
    report = plan_prune(mem, now=NOW)
    capped = [c for c in report.candidates if c.reason == "pattern_cap_exceeded"]
    assert len(capped) == 50  # 250 - 200 cap

    pruned = apply_prune(mem, report)
    assert len(pruned.patterns) == 200
    # The strongest survived: every kept pattern has confidence >= the
    # highest-confidence dropped one.
    dropped_ids = {c.item_id for c in capped}
    kept = [p for p in pruned.patterns]
    max_dropped_conf = max(p.confidence for p in mem.patterns if p.id in dropped_ids)
    assert min(p.confidence for p in kept) >= max_dropped_conf


# ─── Behavioral pattern id stability ─────────────────────────────────────────


def _write_transitions(
    path: Path, entity_id: str, to_state: str, minute_of_day: int, days: int
) -> None:
    """Write `days` transitions for one entity at ~minute_of_day across
    distinct recent days (relative to real now, since the detector reads
    its own clock)."""
    import json

    base = datetime.now(UTC)
    lines = []
    for d in range(1, days + 1):
        ts = (base - timedelta(days=d)).replace(
            hour=minute_of_day // 60, minute=minute_of_day % 60, second=0, microsecond=0
        )
        lines.append(
            json.dumps({"entity_id": entity_id, "from": "on", "to": to_state, "ts": ts.isoformat()})
        )
    (path / "transitions.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_behavioral_pattern_id_stable_under_minute_drift(tmp_path: Path) -> None:
    from mylo.monitor.behavioral import detect_patterns
    from mylo.monitor.transitions import TransitionLogger

    logger = TransitionLogger(tmp_path)
    mem = empty_memory()

    # First run: cluster averaging ~22:31.
    _write_transitions(tmp_path, "light.kitchen", "off", 22 * 60 + 31, days=8)
    first = detect_patterns(logger, mem)
    assert len(first) == 1
    mem.patterns.extend(first)
    pid = first[0].id
    assert pid.endswith("_2230")  # bucketed to the 30-min slot

    # Second run: same behavior, averaged time drifted to ~22:36.
    _write_transitions(tmp_path, "light.kitchen", "off", 22 * 60 + 36, days=8)
    second = detect_patterns(logger, mem)
    # Drift stayed in the same 30-min bucket → existing pattern updated,
    # no new pattern spawned.
    assert second == []
    assert len([p for p in mem.patterns if p.id == pid]) == 1
    assert len(mem.patterns) == 1


# ─── Scratchpad bounds ──────────────────────────────────────────────────────
#
# When nightly merges fail in a streak, nothing drains the scratchpad —
# it used to grow without bound (and the whole file was sent to the LLM
# every night). run_sync now trims the file to a hard cap (overflow is
# archived to history/, never silently deleted) and reads at most
# _SCRATCHPAD_RECONCILE_LIMIT entries into the payload.


def _scratch_line(i: int) -> str:
    return (
        f'- {{type: "user_note", scope: {{general: true}}, '
        f'content: "scratch note {i}", recorded: "2026-08-01", '
        f'confidence: 1.0, conversation_id: "c1"}}'
    )


def test_trim_scratchpad_noop_under_bound(tmp_path: Path) -> None:
    from mylo.memory.scratchpad import trim_scratchpad

    path = tmp_path / "scratchpad.yaml"
    path.write_text("\n".join(_scratch_line(i) for i in range(5)) + "\n")

    removed = trim_scratchpad(tmp_path, max_entries=10, keep=5)

    assert removed == 0
    assert len(path.read_text().strip().splitlines()) == 5
    assert not (tmp_path / "history").exists()


def test_trim_scratchpad_archives_overflow_keeps_newest(tmp_path: Path) -> None:
    from mylo.memory.scratchpad import read_scratchpad, trim_scratchpad

    path = tmp_path / "scratchpad.yaml"
    path.write_text("\n".join(_scratch_line(i) for i in range(20)) + "\n")

    removed = trim_scratchpad(tmp_path, max_entries=10, keep=5)

    assert removed == 15
    kept = read_scratchpad(tmp_path)
    assert len(kept) == 5
    # Entries append chronologically; the newest (highest i) survive.
    assert kept[0].content == "scratch note 19"
    archives = list((tmp_path / "history").glob("scratchpad_overflow_*.yaml"))
    assert len(archives) == 1
    assert len(archives[0].read_text().strip().splitlines()) == 20


def test_trim_scratchpad_missing_file_is_noop(tmp_path: Path) -> None:
    from mylo.memory.scratchpad import trim_scratchpad

    assert trim_scratchpad(tmp_path, max_entries=10, keep=5) == 0


async def test_run_sync_bounds_scratchpad_payload(tmp_path: Path, monkeypatch: Any) -> None:
    from mylo.memory import reconciler as reconciler_mod

    monkeypatch.setattr(reconciler_mod, "_SCRATCHPAD_RECONCILE_LIMIT", 3)

    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()
    (tmp_path / "scratchpad.yaml").write_text("\n".join(_scratch_line(i) for i in range(6)) + "\n")
    provider = _FakeProvider(reply="version: 2\n")

    await run_sync(
        store=store,
        provider=provider,
        registries=None,
        model="claude-haiku-4-5-20251001",
        mylo_data_dir=tmp_path,
        now=NOW,
    )

    assert provider.calls, "reconciler should have called the provider"
    payload = str(provider.calls[0]["messages"])
    assert "scratch note 5" in payload  # newest included
    assert "scratch note 0" not in payload  # oldest excluded by limit
