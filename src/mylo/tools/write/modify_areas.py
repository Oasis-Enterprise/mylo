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

"""``modify_areas`` — create, rename, delete areas, reassign devices/entities.

Pure websocket — no file operations, no reload, no rollback. HA's
area registry is storage-mode and takes effect immediately when the
websocket command succeeds.

Operations:
  create      — ``config/area_registry/create`` with ``name``
  rename      — ``config/area_registry/update`` with ``area_id`` + ``name``
  delete      — ``config/area_registry/delete`` with ``area_id``
  assign_device  — ``config/device_registry/update`` with ``device_id`` + ``area_id``
  assign_entity  — ``config/entity_registry/update`` with ``entity_id`` + ``area_id``
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

Action = Literal["create", "rename", "delete", "assign_device", "assign_entity"]


class ModifyAreasParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    area_id: str | None = Field(
        default=None,
        description=(
            "Required for rename, delete, assign_device, assign_entity. "
            "The target area's id (not display name)."
        ),
    )
    new_name: str | None = Field(
        default=None,
        description="For create: the area name. For rename: the new name.",
    )
    target_ids: list[str] = Field(
        default_factory=list,
        description=(
            "For assign_device / assign_entity: device or entity IDs to move into the area."
        ),
    )


async def handler(params: ModifyAreasParams, ctx: ToolContext) -> ToolResult:
    if params.action == "create":
        if not params.new_name:
            return ToolResult.error("missing_param", "create requires 'new_name'")
        return await _create(ctx, params.new_name)

    if params.action == "rename":
        if not params.area_id or not params.new_name:
            return ToolResult.error("missing_param", "rename requires 'area_id' and 'new_name'")
        return await _rename(ctx, params.area_id, params.new_name)

    if params.action == "delete":
        if not params.area_id:
            return ToolResult.error("missing_param", "delete requires 'area_id'")
        return await _delete(ctx, params.area_id)

    if params.action == "assign_device":
        if not params.area_id or not params.target_ids:
            return ToolResult.error(
                "missing_param", "assign_device requires 'area_id' and 'target_ids'"
            )
        return await _assign_devices(ctx, params.area_id, params.target_ids)

    if params.action == "assign_entity":
        if not params.area_id or not params.target_ids:
            return ToolResult.error(
                "missing_param", "assign_entity requires 'area_id' and 'target_ids'"
            )
        return await _assign_entities(ctx, params.area_id, params.target_ids)

    return ToolResult.error("invalid_action", f"unknown action {params.action!r}")


async def _create(ctx: ToolContext, name: str) -> ToolResult:
    try:
        result = await ctx.ws_client.send_command(
            "config/area_registry/create", write=True, name=name
        )
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")
    area_id = result.get("area_id") if isinstance(result, dict) else None
    return ToolResult.ok({"action": "create", "name": name, "area_id": area_id})


async def _rename(ctx: ToolContext, area_id: str, new_name: str) -> ToolResult:
    try:
        await ctx.ws_client.send_command(
            "config/area_registry/update", write=True, area_id=area_id, name=new_name
        )
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")
    return ToolResult.ok({"action": "rename", "area_id": area_id, "new_name": new_name})


async def _delete(ctx: ToolContext, area_id: str) -> ToolResult:
    try:
        await ctx.ws_client.send_command("config/area_registry/delete", write=True, area_id=area_id)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")
    return ToolResult.ok({"action": "delete", "area_id": area_id})


async def _assign_devices(ctx: ToolContext, area_id: str, device_ids: list[str]) -> ToolResult:
    results: list[dict[str, Any]] = []
    for did in device_ids:
        try:
            await ctx.ws_client.send_command(
                "config/device_registry/update", write=True, device_id=did, area_id=area_id
            )
            results.append({"device_id": did, "ok": True})
        except CommandError as exc:
            results.append({"device_id": did, "ok": False, "error": f"{exc.code}: {exc.message}"})
    all_ok = all(r["ok"] for r in results)
    envelope: dict[str, Any] = {
        "action": "assign_device",
        "area_id": area_id,
        "results": results,
    }
    if not all_ok:
        return ToolResult.error(
            "partial_failure",
            "some device assignments failed — see results",
            data=envelope,
        )
    return ToolResult.ok(envelope)


async def _assign_entities(ctx: ToolContext, area_id: str, entity_ids: list[str]) -> ToolResult:
    results: list[dict[str, Any]] = []
    for eid in entity_ids:
        try:
            await ctx.ws_client.send_command(
                "config/entity_registry/update", write=True, entity_id=eid, area_id=area_id
            )
            results.append({"entity_id": eid, "ok": True})
        except CommandError as exc:
            results.append({"entity_id": eid, "ok": False, "error": f"{exc.code}: {exc.message}"})
    all_ok = all(r["ok"] for r in results)
    envelope: dict[str, Any] = {
        "action": "assign_entity",
        "area_id": area_id,
        "results": results,
    }
    if not all_ok:
        return ToolResult.error(
            "partial_failure",
            "some entity assignments failed — see results",
            data=envelope,
        )
    return ToolResult.ok(envelope)


TOOL = ToolDefinition(
    name="modify_areas",
    description=(
        "Create, rename, or delete areas. Reassign devices and entities "
        "between areas. All operations take effect immediately via HA's "
        "websocket API (no file writes or reloads). Requires approval."
    ),
    params_model=ModifyAreasParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
