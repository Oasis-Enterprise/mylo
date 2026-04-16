"""Tests for M8a memory layer: schema, store round-trip, scratchpad
parsing, and system-prompt integration.
"""

from __future__ import annotations

from pathlib import Path

from mylo.context.memory_injection import build_memory_section
from mylo.memory.schema import (
    HouseholdMember,
    KnownIssue,
    MemoryFile,
    Note,
    empty_memory,
)
from mylo.memory.scratchpad import read_scratchpad, summarize_entries
from mylo.memory.store import MemoryStore

# ─── Schema ──────────────────────────────────────────────────────────────────


def test_empty_memory_is_valid() -> None:
    mem = empty_memory()
    assert mem.version == 2
    assert mem.notes == []
    assert mem.household.members == []


def test_memory_round_trips_through_model_dump() -> None:
    source = MemoryFile(
        notes=[Note(id="n1", content="Basement motion sensor is unreliable", area="basement")],
        known_issues=[KnownIssue(id="i1", description="Zigbee mesh weak")],
    )
    dumped = source.model_dump()
    reparsed = MemoryFile.model_validate(dumped)
    assert reparsed.notes[0].content == source.notes[0].content
    assert reparsed.known_issues[0].description == "Zigbee mesh weak"


def test_unknown_sections_pass_through() -> None:
    # extra="allow" on MemoryFile — user-added fields shouldn't blow up.
    raw = {"version": 2, "notes": [], "custom_field": {"x": 1}}
    mem = MemoryFile.model_validate(raw)
    assert mem.version == 2


# ─── Store ───────────────────────────────────────────────────────────────────


async def test_store_load_creates_empty_file(tmp_path: Path) -> None:
    store = MemoryStore(mylo_data_dir=tmp_path)
    loaded = await store.load()
    assert loaded.version == 2
    assert store.path.exists()


async def test_store_save_snapshots_prior_version(tmp_path: Path) -> None:
    store = MemoryStore(mylo_data_dir=tmp_path)
    await store.load()
    first = empty_memory()
    await store.save(first, note="first save")

    updated = empty_memory()
    updated.notes.append(Note(id="x1", content="test note"))
    await store.save(updated, note="add note")

    # history/ should have at least one snapshot from before the second save.
    snapshots = list((tmp_path / "history").iterdir())
    assert len(snapshots) >= 1

    reloaded = await MemoryStore(mylo_data_dir=tmp_path).load()
    assert any(n.id == "x1" for n in reloaded.notes)


async def test_store_tolerates_malformed_yaml(tmp_path: Path) -> None:
    store = MemoryStore(mylo_data_dir=tmp_path)
    # Write garbage to context.yaml
    store.mylo_data_dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("::: not valid yaml :::")
    # load should fall back to empty, not raise.
    loaded = await store.load()
    assert loaded.version == 2


# ─── Scratchpad ──────────────────────────────────────────────────────────────


def test_read_scratchpad_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_scratchpad(tmp_path) == []


def test_read_scratchpad_parses_memory_note_entries(tmp_path: Path) -> None:
    scratchpad = tmp_path / "scratchpad.yaml"
    scratchpad.write_text(
        '- {type: "user_note", scope: {entity: "cover.garage_door"}, '
        'content: "Sticks sometimes", recorded: "2026-04-15T00:00:00Z", '
        'confidence: 1.0, conversation_id: "c1"}\n'
        '- {type: "preference", scope: {area: "kitchen"}, '
        'content: "Prefers warm light at night", recorded: "2026-04-15T01:00:00Z", '
        'confidence: 0.9, conversation_id: "c1"}\n'
    )
    entries = read_scratchpad(tmp_path)
    # Newest first
    assert entries[0].type == "preference"
    assert entries[1].scope.get("entity") == "cover.garage_door"


def test_summarize_entries_renders_human_readable(tmp_path: Path) -> None:
    from mylo.memory.scratchpad import ScratchpadEntry

    entries = [
        ScratchpadEntry(
            type="user_note",
            content="Basement door sticks",
            scope={"entity": "cover.basement_door"},
            recorded=None,
            confidence=None,
            conversation_id=None,
        )
    ]
    out = summarize_entries(entries)
    assert "Basement door sticks" in out
    assert "cover.basement_door" in out


# ─── System prompt integration ──────────────────────────────────────────────


def test_build_memory_section_always_includes_current_time(tmp_path: Path) -> None:
    # Even with an empty memory, we include CURRENT TIME so time-based
    # rules can fire without tool calls.
    section = build_memory_section(empty_memory(), mylo_data_dir=tmp_path)
    assert "CURRENT TIME:" in section


def test_build_memory_section_includes_notes(tmp_path: Path) -> None:
    mem = MemoryFile(
        notes=[
            Note(
                id="n1",
                content="Basement motion sensor is unreliable",
                area="basement",
            )
        ]
    )
    section = build_memory_section(mem, mylo_data_dir=tmp_path)
    assert "YOUR MEMORY OF THIS HOME" in section
    assert "Basement motion sensor is unreliable" in section
    assert "basement" in section


def test_build_memory_section_includes_scratchpad(tmp_path: Path) -> None:
    (tmp_path / "scratchpad.yaml").write_text(
        '- {type: "user_note", scope: {general: true}, '
        'content: "Prefers dark theme", recorded: "2026-04-15", '
        'confidence: 1.0, conversation_id: "c1"}\n'
    )
    section = build_memory_section(empty_memory(), mylo_data_dir=tmp_path)
    assert "RECENT USER NOTES" in section
    assert "Prefers dark theme" in section


def test_build_memory_section_includes_household(tmp_path: Path) -> None:
    from mylo.memory.schema import Household

    mem = MemoryFile(
        household=Household(
            members=[
                HouseholdMember(
                    name="Maxwell",
                    role="primary_user",
                    notes=["Works from home most days"],
                )
            ]
        )
    )
    section = build_memory_section(mem, mylo_data_dir=tmp_path)
    assert "Maxwell" in section
    assert "primary_user" in section
    assert "Works from home most days" in section
