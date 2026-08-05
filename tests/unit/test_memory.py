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


def test_note_accepts_dict_scope_from_reconciler() -> None:
    # Haiku occasionally copies the scratchpad scope-as-dict shape
    # into context.yaml Notes. Validator flattens it instead of
    # raising, so the sync doesn't fail.
    raw = {
        "version": 2,
        "notes": [
            {
                "id": "n1",
                "content": "workshop conversion in progress",
                "scope": {"area": "garage"},
            }
        ],
    }
    mem = MemoryFile.model_validate(raw)
    assert mem.notes[0].area == "garage"
    assert mem.notes[0].scope is None


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


def test_build_memory_section_empty_memory_returns_empty(tmp_path: Path) -> None:
    # Current time moved to user message prefix (Method 2 token
    # optimization). Empty memory with no household/prefs/notes
    # returns empty string to avoid wasting tokens.
    section = build_memory_section(empty_memory(), mylo_data_dir=tmp_path)
    assert section == ""


def test_render_current_time_returns_timestamp() -> None:
    from mylo.context.memory_injection import render_current_time

    result = render_current_time(None)
    assert "CURRENT TIME:" in result


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


def test_build_memory_section_renders_layout_and_theme_prefs(tmp_path: Path) -> None:
    """Regression: layout_preference was stored but never rendered into
    the prompt, so a recorded preference silently did nothing."""
    from mylo.memory.schema import DashboardPreferences, Preferences

    mem = MemoryFile(
        preferences=Preferences(
            dashboard=DashboardPreferences(
                card_style="mushroom",
                layout_preference="sections",
                theme="noctis",
            )
        )
    )
    section = build_memory_section(mem, mylo_data_dir=tmp_path)
    assert "layout=sections" in section
    assert "theme=noctis" in section
    assert "style=mushroom" in section


def test_preferences_empty_counts_layout_and_theme() -> None:
    """Regression: the reconciler's wipe-protection guard ignored
    layout_preference (and theme), so a memory holding only those could
    be silently dropped."""
    from mylo.memory.reconciler import _preferences_empty
    from mylo.memory.schema import DashboardPreferences, Preferences

    only_layout = MemoryFile(
        preferences=Preferences(dashboard=DashboardPreferences(layout_preference="sections"))
    )
    assert _preferences_empty(only_layout) is False

    only_theme = MemoryFile(preferences=Preferences(dashboard=DashboardPreferences(theme="noctis")))
    assert _preferences_empty(only_theme) is False

    assert _preferences_empty(MemoryFile()) is True
