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

"""``query_system`` — system health and inventory.

Four scopes:

* ``overview`` — HA core version, state/entity counts, uptime-ish info from
  registries.
* ``integrations`` — loaded integrations with entity counts.
* ``addons`` — Supervisor add-on list (requires Supervisor; omitted with a
  note when we're running outside the add-on).
* ``hardware`` — host hardware info from Supervisor (same caveat).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

Scope = Literal["overview", "integrations", "addons", "hardware"]


class QuerySystemParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Scope = Field(default="overview", description="Which system info to return.")


async def _overview(ctx: ToolContext) -> ToolResult:
    try:
        config = await ctx.ws_client.send_command("get_config")
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")
    reg = ctx.registries
    data = {
        "ha_version": config.get("version") if isinstance(config, dict) else None,
        "location_name": config.get("location_name") if isinstance(config, dict) else None,
        "time_zone": config.get("time_zone") if isinstance(config, dict) else None,
        "unit_system": config.get("unit_system") if isinstance(config, dict) else None,
        "entities": len(reg.entities),
        "devices": len(reg.devices),
        "areas": len(reg.areas),
        "labels": len(reg.labels),
    }
    return ToolResult.ok(data)


async def _integrations(ctx: ToolContext) -> ToolResult:
    counts: dict[str, int] = {}
    for e in ctx.registries.entities.values():
        if e.platform:
            counts[e.platform] = counts.get(e.platform, 0) + 1
    items = [
        {"domain": platform, "entity_count": n}
        for platform, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return ToolResult.ok({"count": len(items), "integrations": items})


async def _addons(ctx: ToolContext) -> ToolResult:
    if not ctx.config.supervisor_token:
        return ToolResult.error(
            "unavailable",
            "add-on info requires the Supervisor token (running as an HA add-on)",
        )
    try:
        raw = await ctx.ws_client.send_command("supervisor/api", endpoint="/addons", method="get")
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")
    if not isinstance(raw, dict):
        return ToolResult.error("unexpected_response", "supervisor response not a dict")
    data = raw.get("data") or {}
    addons = data.get("addons") or []
    shaped = []
    for a in addons:
        if not isinstance(a, dict):
            continue
        shaped.append(
            {
                "slug": a.get("slug"),
                "name": a.get("name"),
                "version": a.get("version"),
                "state": a.get("state"),
                "update_available": a.get("update_available"),
            }
        )
    return ToolResult.ok({"count": len(shaped), "addons": shaped})


async def _hardware(ctx: ToolContext) -> ToolResult:
    if not ctx.config.supervisor_token:
        return ToolResult.error(
            "unavailable",
            "hardware info requires the Supervisor token (running as an HA add-on)",
        )
    try:
        raw = await ctx.ws_client.send_command(
            "supervisor/api", endpoint="/host/info", method="get"
        )
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")
    if not isinstance(raw, dict):
        return ToolResult.error("unexpected_response", "supervisor response not a dict")
    data = raw.get("data") or {}
    return ToolResult.ok(
        {
            "hostname": data.get("hostname"),
            "operating_system": data.get("operating_system"),
            "kernel": data.get("kernel"),
            "deployment": data.get("deployment"),
            "disk_total": data.get("disk_total"),
            "disk_used": data.get("disk_used"),
            "disk_free": data.get("disk_free"),
        }
    )


_DISPATCH: dict[str, Callable[[ToolContext], Awaitable[ToolResult]]] = {
    "overview": _overview,
    "integrations": _integrations,
    "addons": _addons,
    "hardware": _hardware,
}


async def handler(params: QuerySystemParams, ctx: ToolContext) -> ToolResult:
    return await _DISPATCH[params.scope](ctx)


TOOL = ToolDefinition(
    name="query_system",
    description=(
        "System health and inventory. Scopes: 'overview' (HA version, counts), "
        "'integrations' (entity counts per integration), 'addons' (Supervisor "
        "add-on list), 'hardware' (host info). Add-on and hardware scopes "
        "require Mylo to be running as an HA add-on with the Supervisor token."
    ),
    params_model=QuerySystemParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
