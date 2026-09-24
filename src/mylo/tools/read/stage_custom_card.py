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

"""``stage_custom_card`` — check a model-written card and hold it for Apply."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.dashboard.cards import (
    CardStore,
    StagedCard,
    card_hash,
    card_url,
    check_card_source,
)
from mylo.files.manager import exists, read_text
from mylo.safety.file_access import FileAccessError, resolve_custom_card_path
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class StageCustomCardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element: str = Field(
        description="Custom element name, e.g. 'mylo-game-row'. Must start with 'mylo-'."
    )
    description: str = Field(
        min_length=1,
        max_length=160,
        description="One line: what the card shows and what config it takes.",
    )
    source: str = Field(
        min_length=1, description="The complete JavaScript module (see the contract in the prompt)."
    )
    config_example: dict[str, Any] | None = Field(
        default=None, description="A card config using this element, shown to the user."
    )


async def handler(params: StageCustomCardParams, ctx: ToolContext) -> ToolResult:
    if ctx.cards is None:
        return ToolResult.error("cards_unavailable", "card store not configured")

    issues = check_card_source(params.element, params.source)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        return ToolResult.error(
            "card_invalid",
            f"{len(errors)} contract violation(s) — fix every listed issue and stage again",
            data={"issues": [i.to_dict() for i in issues]},
        )
    try:
        path = resolve_custom_card_path(ctx.config.ha_config_dir, params.element)
    except FileAccessError as exc:
        return ToolResult.error(
            "card_invalid",
            exc.message,
            data={"issues": [{"code": exc.code, "message": exc.message, "severity": "error"}]},
        )

    previous = read_text(path) if exists(path) else None
    if previous is not None and previous == params.source:
        return ToolResult.error(
            "card_unchanged", "the staged source is identical to the file on disk"
        )

    content_hash = card_hash(params.source)
    card = StagedCard(
        card_id=CardStore.new_id(),
        element=params.element,
        path=str(path),
        url=card_url(params.element, content_hash),
        source=params.source,
        previous_source=previous,
        hash=content_hash,
        action="update" if previous is not None else "create",
        description=params.description,
        config_example=params.config_example,
        warnings=[i.to_dict() for i in issues],
        created_at=datetime.now(UTC),
        conversation_id=ctx.conversation_id,
    )
    ctx.cards.put(card)
    return ToolResult.ok(
        {
            "preview": True,
            "card_id": card.card_id,
            "element": card.element,
            "action": card.action,
            "url": card.url,
            "line_count": len(params.source.splitlines()),
            "byte_count": len(params.source.encode("utf-8")),
            "warnings": card.warnings,
            "previous_source": previous,
            "source": params.source,
            "config_example": params.config_example,
            "description": params.description,
            "note": (
                f"Staged. Reference it as custom:{card.element} in plan_dashboard. Do not call "
                "apply_custom_card until the user's next message approves."
            ),
        }
    )


TOOL = ToolDefinition(
    name="stage_custom_card",
    description=(
        "Stage a custom Lovelace card's JavaScript for approval. The source "
        "must follow the card contract (HTMLElement in <ha-card>, setConfig "
        "+ set hass, guarded define of a mylo-* element, window.customCards "
        "entry, no imports/fetch/eval). Returns card_id and config_example. "
        "Nothing is written."
    ),
    params_model=StageCustomCardParams,
    tier=Tier.READ,
    handler=handler,
    cacheable=False,
)
register(TOOL)
