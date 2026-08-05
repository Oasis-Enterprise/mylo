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

"""``query_dashboard_env`` — installed themes and custom cards.

One call tells the model what it can safely build with: which frontend
themes exist (so a theme choice is never a guess) and which ``custom:*``
card types the registered lovelace resources actually provide (so a
generated dashboard never renders "Custom element doesn't exist").
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from mylo.ha.lovelace_meta import detect_custom_cards, get_resources, get_themes
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class QueryDashboardEnvParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def handler(params: QueryDashboardEnvParams, ctx: ToolContext) -> ToolResult:
    themes = await get_themes(ctx.ws_client)
    resources = await get_resources(ctx.ws_client)

    data: dict[str, Any] = {
        "themes": themes.names if themes else None,
        "default_theme": themes.default if themes else None,
        "resource_count": len(resources) if resources is not None else None,
        "custom_cards_detected": (
            sorted(detect_custom_cards(resources)) if resources is not None else None
        ),
    }
    if resources is None:
        data["note"] = (
            "lovelace resources are not listable (YAML mode or older HA) — "
            "custom card availability is unknown, prefer native HA cards"
        )
    return ToolResult.ok(data)


TOOL = ToolDefinition(
    name="query_dashboard_env",
    description=(
        "Discover the dashboard environment before building: installed "
        "frontend themes (use one of these for the view 'theme' — never "
        "guess) and which custom:* card types are actually installed via "
        "lovelace resources. Only use custom cards from "
        "custom_cards_detected; when a card isn't listed or the list is "
        "null, use native HA cards (tile, heading, entities) instead. "
        "Call this once at the start of any dashboard build."
    ),
    params_model=QueryDashboardEnvParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
