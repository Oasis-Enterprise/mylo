"""``manage_notification_filters`` — add, remove, list notification suppressions.

When a user says "stop notifying me about stale automations", the
model calls this tool to add a suppression rule. The background
notifier checks these rules before every send — pure Python, no
LLM cost.

Suppression types match the notification IDs used by the hourly
monitor:
- ``stale_automation`` — automations that haven't fired in >48h
- ``unavailable`` — entities that went unavailable
- ``anomaly`` — z-score anomaly alerts
- ``sync_conflict`` — memory sync conflict alerts
- ``*`` — suppress ALL proactive notifications
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.logging_setup import get_logger
from mylo.memory.schema import NotificationSuppression
from mylo.memory.store import MemoryStore
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)

Action = Literal["add", "remove", "list"]


class ManageNotificationFiltersParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(
        description="'list' shows current filters. 'add' creates a new suppression. 'remove' deletes one.",
    )
    type: str | None = Field(
        default=None,
        description=(
            "Notification type: 'stale_automation', 'unavailable', 'anomaly', "
            "'sync_conflict', or '*' for all. Required for add/remove."
        ),
    )
    entity: str | None = Field(
        default=None,
        description="Specific entity_id to suppress. Null = suppress all of this type.",
    )
    reason: str | None = Field(
        default=None,
        description="Why (shown in Memory tab). Only for 'add'.",
    )


async def handler(params: ManageNotificationFiltersParams, ctx: ToolContext) -> ToolResult:
    store = MemoryStore(mylo_data_dir=ctx.config.mylo_data_dir)
    await store.load()
    memory = store.current()

    if params.action == "list":
        return ToolResult.ok(
            {
                "filters": [
                    {
                        "type": s.type,
                        "entity": s.entity,
                        "reason": s.reason,
                    }
                    for s in memory.notification_suppressions
                ],
                "count": len(memory.notification_suppressions),
            }
        )

    if not ctx.user_approved:
        return ToolResult.error(
            "confirmation_required",
            f"'{params.action}' requires user approval — click Apply",
        )

    if not params.type:
        return ToolResult.error("missing_param", f"'{params.action}' requires 'type'")

    if params.action == "add":
        # Check for duplicates.
        for existing in memory.notification_suppressions:
            if existing.type == params.type and existing.entity == params.entity:
                return ToolResult.ok(
                    {
                        "action": "add",
                        "already_exists": True,
                        "type": params.type,
                        "entity": params.entity,
                    }
                )

        memory.notification_suppressions.append(
            NotificationSuppression(
                type=params.type,
                entity=params.entity,
                reason=params.reason,
                added=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        )
        await store.save(memory, note=f"suppress {params.type} notifications")
        return ToolResult.ok(
            {
                "action": "add",
                "type": params.type,
                "entity": params.entity,
                "total_filters": len(memory.notification_suppressions),
            }
        )

    if params.action == "remove":
        before = len(memory.notification_suppressions)
        memory.notification_suppressions = [
            s
            for s in memory.notification_suppressions
            if not (s.type == params.type and s.entity == params.entity)
        ]
        removed = before - len(memory.notification_suppressions)
        if removed == 0:
            return ToolResult.error(
                "not_found", f"no filter matching type={params.type} entity={params.entity}"
            )
        await store.save(memory, note=f"unsuppress {params.type} notifications")
        return ToolResult.ok(
            {
                "action": "remove",
                "removed": removed,
                "total_filters": len(memory.notification_suppressions),
            }
        )

    return ToolResult.error("invalid_action", f"unknown action {params.action!r}")


TOOL = ToolDefinition(
    name="manage_notification_filters",
    description=(
        "Add, remove, or list notification suppression rules. When a user "
        "says 'stop notifying me about stale automations', add a filter "
        "with type='stale_automation'. Types: stale_automation, unavailable, "
        "anomaly, sync_conflict, or * for all. Optionally scope to a "
        "specific entity_id. 'list' is free; add/remove require approval."
    ),
    params_model=ManageNotificationFiltersParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
