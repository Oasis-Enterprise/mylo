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

"""Structured diff for YAML dry-run previews.

The naïve ``difflib.unified_diff`` of YAML serializations is noisy: reflow
from ruamel, quote style flips, insertion of a trailing newline — all
show up as changes even when the semantic content is identical. For LLM
and user review we want structural diffs: "added key X, removed key Y,
changed Z from ... to ...".

:func:`diff_yaml` loads both sides through :mod:`validators.yaml_parser`
and walks the two trees side by side. The result is a flat list of
:class:`DiffEntry` records with dotted paths. Trivial to render as
either a summary count or an expandable tree in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mylo.validators.yaml_parser import load_yaml


class Change(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(slots=True)
class DiffEntry:
    path: str
    change: Change
    old: Any = None
    new: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "change": self.change.value}
        if self.change is not Change.ADDED:
            out["old"] = _compact(self.old)
        if self.change is not Change.REMOVED:
            out["new"] = _compact(self.new)
        return out


@dataclass(slots=True)
class DiffResult:
    entries: list[DiffEntry] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        out = {"added": 0, "removed": 0, "changed": 0}
        for e in self.entries:
            out[e.change.value] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "entries": [e.to_dict() for e in self.entries],
        }


def diff_yaml(old_text: str | None, new_text: str) -> DiffResult:
    """Diff two YAML documents structurally.

    ``old_text`` may be ``None`` to represent a brand-new file — every
    top-level entry is then :attr:`Change.ADDED`.
    """
    old = load_yaml(old_text) if old_text else None
    new = load_yaml(new_text)
    result = DiffResult()
    _walk(old, new, "", result)
    return result


def diff_structs(old: Any, new: Any) -> DiffResult:
    """Diff two already-parsed Python objects. Handy when a write tool
    already has the structs and doesn't want to re-serialize.
    """
    result = DiffResult()
    _walk(old, new, "", result)
    return result


# ─── Walk ───────────────────────────────────────────────────────────────────


def _walk(old: Any, new: Any, path: str, result: DiffResult) -> None:
    if _equal(old, new):
        return

    # A brand-new document (old is None) should produce one ADDED per top-
    # level key/index, not a single root-level CHANGED. Same when a
    # document is deleted. Normalize by recursing into an empty peer.
    if old is None and isinstance(new, dict):
        _walk({}, new, path, result)
        return
    if old is None and isinstance(new, list):
        _walk([], new, path, result)
        return
    if new is None and isinstance(old, dict):
        _walk(old, {}, path, result)
        return
    if new is None and isinstance(old, list):
        _walk(old, [], path, result)
        return

    if isinstance(old, dict) and isinstance(new, dict):
        keys = set(old) | set(new)
        for k in sorted(keys, key=str):
            sub_path = f"{path}.{k}" if path else str(k)
            if k not in old:
                result.entries.append(DiffEntry(sub_path, Change.ADDED, new=new[k]))
            elif k not in new:
                result.entries.append(DiffEntry(sub_path, Change.REMOVED, old=old[k]))
            else:
                _walk(old[k], new[k], sub_path, result)
        return

    if isinstance(old, list) and isinstance(new, list):
        # Index-based — good enough for automation lists where order matters.
        for i in range(max(len(old), len(new))):
            sub_path = f"{path}[{i}]"
            if i >= len(old):
                result.entries.append(DiffEntry(sub_path, Change.ADDED, new=new[i]))
            elif i >= len(new):
                result.entries.append(DiffEntry(sub_path, Change.REMOVED, old=old[i]))
            else:
                _walk(old[i], new[i], sub_path, result)
        return

    result.entries.append(DiffEntry(path or "(root)", Change.CHANGED, old=old, new=new))


def _equal(a: Any, b: Any) -> bool:
    """Python equality that accounts for ruamel's CommentedMap/Seq and our
    PreservedTag being equal to plain types with the same content.
    """
    if a is b:
        return True
    if a is None or b is None:
        return a is b
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return False
        return all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def _compact(value: Any) -> Any:
    """Convert ruamel CommentedMap/Seq to plain dicts/lists and truncate
    long strings so diff output stays readable in the UI.
    """
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    if isinstance(value, str) and len(value) > 200:
        return value[:197] + "..."
    return value
