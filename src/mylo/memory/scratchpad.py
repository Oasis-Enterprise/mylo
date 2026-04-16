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
