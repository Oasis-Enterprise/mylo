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

"""Read the in-session scratchpad that ``memory_note`` writes to.

Per spec §3.9, the scratchpad is Mylo's "immediate memory" — notes
captured mid-conversation that need to be available RIGHT AWAY,
before the next reconciliation window. We append-only via the
``memory_note`` tool's minimal inline-YAML writer; here we parse
that back so the chat prompt can include the notes verbatim.

Format per ``memory_note.py`` — each line begins with ``- `` and
contains a JSON-like inline YAML mapping. Tolerant parsing: a
corrupt line is skipped, not fatal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mylo.logging_setup import get_logger
from mylo.validators.yaml_parser import load_yaml

log = get_logger(__name__)


@dataclass(slots=True)
class ScratchpadEntry:
    type: str  # user_note | preference | observation | issue
    content: str
    scope: dict[str, Any]
    recorded: str | None
    confidence: float | None
    conversation_id: str | None


def read_scratchpad(mylo_data_dir: Path, *, limit: int | None = None) -> list[ScratchpadEntry]:
    """Load entries. Returns newest first. Missing file → empty list."""
    path = mylo_data_dir / "scratchpad.yaml"
    if not path.exists():
        return []
    try:
        parsed = load_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("memory.scratchpad_parse_failed", error=str(exc))
        return []
    if not isinstance(parsed, list):
        return []
    entries: list[ScratchpadEntry] = []
    for raw in parsed:
        if not isinstance(raw, dict):
            continue
        scope = raw.get("scope")
        entries.append(
            ScratchpadEntry(
                type=str(raw.get("type") or "user_note"),
                content=str(raw.get("content") or ""),
                scope=dict(scope) if isinstance(scope, dict) else {},
                recorded=raw.get("recorded"),
                confidence=raw.get("confidence"),
                conversation_id=raw.get("conversation_id"),
            )
        )
    entries.reverse()  # newest first
    if limit is not None:
        entries = entries[:limit]
    return entries


def trim_scratchpad(mylo_data_dir: Path, *, max_entries: int = 500, keep: int = 300) -> int:
    """Hard-cap the scratchpad file, archiving the overflow.

    Normally a successful reconciliation drains the file — but a streak
    of failed merges never drains it, and the file (and the nightly LLM
    payload built from it) grew without bound in production. When the
    file exceeds ``max_entries`` lines, the FULL file is archived to
    ``history/scratchpad_overflow_<ts>.yaml`` (entries beyond the cap
    are recoverable there, never silently deleted) and the newest
    ``keep`` lines are kept in place.

    Line-based on purpose: the file is one ``- {...}`` inline-YAML entry
    per line (memory_note's writer), and rewriting through a YAML dump
    would risk format drift. Returns the number of lines removed.
    """
    path = mylo_data_dir / "scratchpad.yaml"
    if not path.exists():
        return 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) <= max_entries:
        return 0

    history_dir = mylo_data_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    dest = history_dir / f"scratchpad_overflow_{stamp}.yaml"
    try:
        dest.write_bytes(path.read_bytes())
    except OSError as exc:
        log.warning("memory.scratchpad_archive_failed", error=str(exc))
        return 0  # don't discard entries we failed to archive

    kept = lines[-keep:]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    removed = len(lines) - len(kept)
    log.warning("memory.scratchpad_trimmed", removed=removed, kept=len(kept), archive=dest.name)
    return removed


def drain_scratchpad(mylo_data_dir: Path) -> int:
    """Remove ``scratchpad.yaml`` after a successful reconciliation.

    Per spec §3.9 the reconciler drains the scratchpad — otherwise
    entries would re-merge on every sync and create duplicates. We
    archive the drained contents to ``history/scratchpad_<ts>.yaml``
    so a debug trail survives. Returns the number of entries drained.
    """
    path = mylo_data_dir / "scratchpad.yaml"
    if not path.exists():
        return 0
    entries = read_scratchpad(mylo_data_dir)
    history_dir = mylo_data_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    dest = history_dir / f"scratchpad_{stamp}.yaml"
    try:
        dest.write_bytes(path.read_bytes())
    except OSError as exc:
        log.warning("memory.scratchpad_archive_failed", error=str(exc))
    try:
        path.unlink()
    except OSError as exc:
        log.warning("memory.scratchpad_drain_failed", error=str(exc))
    return len(entries)


def summarize_entries(entries: list[ScratchpadEntry]) -> str:
    """Render a compact plain-text summary for the system prompt."""
    if not entries:
        return ""
    lines: list[str] = []
    for e in entries:
        scope_bits: list[str] = []
        if e.scope.get("entity"):
            scope_bits.append(f"entity={e.scope['entity']}")
        if e.scope.get("area"):
            scope_bits.append(f"area={e.scope['area']}")
        if e.scope.get("general"):
            scope_bits.append("general")
        scope_str = f" ({', '.join(scope_bits)})" if scope_bits else ""
        lines.append(f"- [{e.type}{scope_str}] {e.content}")
    return "\n".join(lines)
