"""``memory_note`` — record a note to Mylo's scratchpad.

The full memory system lands in Milestone 8. For M2 this tool is a durable
stub: it appends to ``/config/.mylo/scratchpad.yaml`` so that when the
reconciler ships it has real data to work with. The user-facing contract is
already in place: calling this tool with a structured payload records an
item that the nightly sync will later reconcile into ``context.yaml``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.logging_setup import get_logger
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)

NoteType = Literal["user_note", "preference", "observation", "issue"]


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str | None = None
    area: str | None = None
    general: bool | None = None


class MemoryNoteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: NoteType
    scope: Scope = Field(default_factory=Scope)
    content: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="For observations only.")


def _scope_dict(scope: Scope) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if scope.entity:
        out["entity"] = scope.entity
    if scope.area:
        out["area"] = scope.area
    if scope.general:
        out["general"] = True
    return out


async def handler(params: MemoryNoteParams, ctx: ToolContext) -> ToolResult:
    scratchpad = ctx.config.mylo_data_dir / "scratchpad.yaml"
    scratchpad.parent.mkdir(parents=True, exist_ok=True)

    entry: dict[str, Any] = {
        "type": params.type,
        "scope": _scope_dict(params.scope),
        "content": params.content,
        "confidence": params.confidence,
        "recorded": datetime.now(UTC).isoformat(),
        "conversation_id": ctx.conversation_id,
    }

    # The scratchpad is an append-only YAML list. Until ruamel.yaml lands with
    # the memory system (M8), we use a minimal append: each entry is written
    # as a YAML stanza prefixed with "- " on its own line block. This is
    # valid YAML when concatenated with an existing list under the same key,
    # and the M8 reconciler will normalize the file.
    lines = ["- " + _to_yaml_inline(entry) + "\n"]
    with scratchpad.open("a", encoding="utf-8") as f:
        f.writelines(lines)

    log.info(
        "memory.scratchpad.appended",
        type=params.type,
        entity=params.scope.entity,
        area=params.scope.area,
    )
    return ToolResult.ok({"recorded": True, "scratchpad_path": str(scratchpad)})


def _to_yaml_inline(obj: Any) -> str:
    """Render a dict as a one-line YAML flow mapping.

    Safe for nested dicts/strings/numbers/bools/None. Not general-purpose —
    this scratchpad only ever stores the structure above.
    """
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        return str(obj)
    if isinstance(obj, str):
        # Quote strings to avoid ambiguity with YAML special values.
        escaped = obj.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(obj, dict):
        parts = [f"{k}: {_to_yaml_inline(v)}" for k, v in obj.items()]
        return "{" + ", ".join(parts) + "}"
    if isinstance(obj, list):
        return "[" + ", ".join(_to_yaml_inline(v) for v in obj) + "]"
    return f'"{obj!s}"'


TOOL = ToolDefinition(
    name="memory_note",
    description=(
        "Record a note, preference, observation, or known issue to Mylo's "
        "memory. Use when the user shares information worth remembering or "
        "when you observe something noteworthy. Notes are reconciled into "
        "the master memory file on the next sync."
    ),
    params_model=MemoryNoteParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
