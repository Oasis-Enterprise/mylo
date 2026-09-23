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

"""``apply_custom_card`` — write a staged card and register its resource."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mylo.dashboard.cards import ensure_card_resource
from mylo.files.backup import take_backup
from mylo.files.manager import atomic_write
from mylo.ha.ws_client import CommandError
from mylo.logging_setup import get_logger
from mylo.tools import executor as tool_executor
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)


class ApplyCustomCardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str = Field(
        min_length=1, description="card_id from stage_custom_card, after the user clicked Apply."
    )


async def handler(params: ApplyCustomCardParams, ctx: ToolContext) -> ToolResult:
    # Deliberately requires the real card_id — unlike the executor's scoped-
    # approval gate, this check does not honour the in-process "*" wildcard.
    if params.card_id not in ctx.approved_plan_ids:
        return ToolResult.error(
            "card_not_approved",
            "this card was not approved by the user — present it and wait for Apply",
        )
    if ctx.cards is None:
        return ToolResult.error("cards_unavailable", "card store not configured")
    card = ctx.cards.get(params.card_id)
    if card is None or card.conversation_id != ctx.conversation_id:
        return ToolResult.error("card_not_found", "staged card expired or unknown — stage it again")

    path = Path(card.path)
    backup_path: str | None = None
    try:
        handle = take_backup(path, ctx.config.ha_config_dir, ctx.config.mylo_data_dir)
        backup_path = str(handle.backup_path) if handle.backup_path else None
        atomic_write(path, card.source)
    except OSError as exc:
        return ToolResult.error("write_failed", f"could not write {path}: {exc}")

    try:
        resource_id, action = await ensure_card_resource(ctx.ws_client, card.element, card.url)
    except CommandError as exc:
        return ToolResult.error(
            "resource_failed",
            f"{exc.code}: {exc.message}",
            data={
                "path": str(path),
                "url": card.url,
                "hint": "the file is written; register the resource by hand or retry apply_custom_card",
            },
        )

    ctx.cards.remove(card.card_id)
    tool_executor.invalidate("query_dashboard_env")
    log.info(
        "dashboard.custom_card_applied",
        element=card.element,
        action=action,
        resource_id=resource_id,
    )
    return ToolResult.ok(
        {
            "preview": False,
            "card_id": card.card_id,
            "element": card.element,
            "path": str(path),
            "url": card.url,
            "resource_id": resource_id,
            "resource_action": action,
            "backup": backup_path,
            "note": "Tell the user a hard refresh may be needed the first time a new card loads.",
        }
    )


TOOL = ToolDefinition(
    name="apply_custom_card",
    description=(
        "Write a staged custom card to www/mylo-cards/ and register it as a Lovelace "
        "module resource. Pass the card_id from stage_custom_card after the user clicked "
        "Apply. Call this BEFORE apply_dashboard_plan when a plan uses the card."
    ),
    params_model=ApplyCustomCardParams,
    tier=Tier.MODIFY,
    handler=handler,
    approval_key="card_id",
)
register(TOOL)
