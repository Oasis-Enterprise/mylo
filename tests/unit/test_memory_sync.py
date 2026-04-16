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
from mylo.memory.reconciler import run_sync
from mylo.memory.schema import (
    Claim,
    Conflict,
    HouseholdMember,
    ItemMetadata,
    KnownIssue,
    Note,
    Pattern,
    RejectedSuggestion,
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
    mem.known_issues.append(
        KnownIssue(id="i_resolved", description="fixed", status="resolved")
    )
    mem.known_issues.append(
        KnownIssue(id="i_active", description="still broken", status="active")
    )
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
    mem.rejected.append(
        RejectedSuggestion(id="r_old", suggestion="no thanks", date=iso(200))
    )
    mem.rejected.append(
        RejectedSuggestion(id="r_new", suggestion="still relevant", date=iso(30))
    )
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


async def test_sync_endpoint_apply_prune_flag(
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


async def test_delete_memory_item_removes_note(
    aiohttp_client, memory_app: web.Application
) -> None:
    from mylo.server.app import AppKeys

    store = memory_app[AppKeys.MEMORY]
    mem = await store.load()
    mem.notes.append(Note(id="n_drop", content="bye"))
    mem.notes.append(Note(id="n_keep", content="stays"))
    await store.save(mem, note="seed")

    client = await aiohttp_client(memory_app)
    resp = await client.delete(
        "/api/memory/item", json={"section": "notes", "id": "n_drop"}
    )
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
    resp = await client.delete(
        "/api/memory/item", json={"section": "household", "id": "anyone"}
    )
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


async def test_prune_only_honors_ids_filter(
    aiohttp_client, memory_app: web.Application
) -> None:
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
    resp = await client.post(
        "/api/memory/conflict/conflict_abc/resolve", json={"choice": "b"}
    )
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
    resp = await client.post(
        "/api/memory/conflict/nope/resolve", json={"choice": "a"}
    )
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
