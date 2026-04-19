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

"""``reload_config`` — tier-3 tool that triggers a YAML reload.

Maps a friendly ``scope`` to the real ``homeassistant.reload_*`` service
via :data:`mylo.files.rollback.RELOAD_SERVICES`. Used when a user has
edited a file manually and wants Mylo to pick it up, or when the write
loop needs an explicit reload separate from its own sequence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.rollback import RELOAD_SERVICES
from mylo.ha.ws_client import CommandError, CommandTimeout
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class ReloadConfigParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(
        description=(
            "Domain to reload — 'automation', 'script', 'scene', 'template', "
            "'input_boolean', 'lovelace', 'all', etc. See the docs for the "
            "full list."
        ),
    )


async def handler(params: ReloadConfigParams, ctx: ToolContext) -> ToolResult:
    if params.scope not in RELOAD_SERVICES:
        return ToolResult.error(
            "unknown_scope",
            f"scope must be one of {sorted(RELOAD_SERVICES)}, got {params.scope!r}",
        )

    domain, service = RELOAD_SERVICES[params.scope]
    try:
        await ctx.ws_client.send_command("call_service", domain=domain, service=service)
    except CommandTimeout as exc:
        return ToolResult.error("ha_timeout", str(exc))
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({"scope": params.scope, "called": f"{domain}.{service}"})


TOOL = ToolDefinition(
    name="reload_config",
    description=(
        "Reload a specific HA configuration domain. Use when the user has "
        "manually edited a config file and wants HA to pick up the change. "
        "Requires approval; rate-limited."
    ),
    params_model=ReloadConfigParams,
    tier=Tier.ACTION,
    handler=handler,
)
register(TOOL)
