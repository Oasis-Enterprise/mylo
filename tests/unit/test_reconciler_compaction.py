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

from __future__ import annotations

from mylo.context.tokens import estimate_tokens
from mylo.memory.reconciler import _reattach_compacted, compact_payload_sections
from mylo.memory.schema import MemoryFile, Note


def test_compaction_drops_low_value_notes_to_fit() -> None:
    mem = MemoryFile()
    mem.notes = [Note(id=f"n{i}", content="x" * 200) for i in range(1000)]
    compacted, marker, dropped = compact_payload_sections(mem, budget_tokens=2000)
    assert len(compacted.notes) < 1000
    assert "compacted" in marker.lower()
    assert len(dropped["notes"]) > 0
    assert estimate_tokens(compacted.model_dump_json()) <= 2000


def test_compaction_preserves_user_confirmed_notes() -> None:
    mem = MemoryFile()
    keep = Note(id="keep", content="y" * 200, source="user_confirmed")
    mem.notes = [keep, *[Note(id=f"n{i}", content="x" * 200) for i in range(1000)]]
    compacted, _, dropped = compact_payload_sections(mem, budget_tokens=1500)
    assert any(n.id == "keep" for n in compacted.notes)
    assert all(n.id != "keep" for n in dropped["notes"])


def test_fast_path_when_already_small() -> None:
    mem = MemoryFile()
    mem.notes = [Note(id="n1", content="hi")]
    compacted, marker, dropped = compact_payload_sections(mem, budget_tokens=100_000)
    assert marker == ""
    assert dropped == {"notes": [], "patterns": []}
    assert len(compacted.notes) == 1


def test_reattach_restores_dropped_without_duplicating() -> None:
    merged = MemoryFile()
    merged.notes = [Note(id="kept", content="a")]
    dropped = {
        "notes": [Note(id="dropped1", content="b"), Note(id="kept", content="dup")],
        "patterns": [],
    }
    _reattach_compacted(merged, dropped)
    ids = sorted(n.id for n in merged.notes)
    assert ids == ["dropped1", "kept"]  # dropped re-added; existing id not duplicated
