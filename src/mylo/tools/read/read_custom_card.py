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

"""``read_custom_card`` — the current source of a Mylo-authored card."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.manager import exists, read_text
from mylo.safety.file_access import FileAccessError, resolve_custom_card_path
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class ReadCustomCardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element: str = Field(
        description="Element name of a card Mylo authored, e.g. 'mylo-game-row' (see query_dashboard_env.mylo_cards)."
    )


async def handler(params: ReadCustomCardParams, ctx: ToolContext) -> ToolResult:
    try:
        path = resolve_custom_card_path(ctx.config.ha_config_dir, params.element)
    except FileAccessError as exc:
        return ToolResult.error(exc.code, exc.message)
    if not exists(path):
        return ToolResult.error("card_not_found", f"no authored card {params.element!r} on disk")
    source = read_text(path)
    return ToolResult.ok(
        {
            "element": params.element,
            "path": str(path),
            "line_count": len(source.splitlines()),
            "byte_count": len(source.encode("utf-8")),
            "source": source,
            "note": "To change it, stage the COMPLETE new source with stage_custom_card; the user sees a diff.",
        }
    )


TOOL = ToolDefinition(
    name="read_custom_card",
    description=(
        "Read the current JavaScript of a card Mylo authored (www/mylo-cards/<element>.js). "
        "Call before updating a card so the change is targeted; then stage the complete new "
        "source with stage_custom_card."
    ),
    params_model=ReadCustomCardParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
