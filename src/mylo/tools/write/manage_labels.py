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

"""``manage_labels`` — create, assign, remove labels on entities/devices.

Pure websocket. Labels in HA are lightweight tags that live in the
entity and device registries. Creating a label is one WS call;
assigning it is an update to the entity/device registry entry.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

Action = Literal["create", "assign", "remove", "list"]


class ManageLabelsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(
        description=(
            "'create' a new label, 'assign' a label to entities/devices, "
            "'remove' a label from entities/devices, 'list' all labels."
        ),
    )
    label: str | None = Field(
        default=None,
        description="Label name (for create) or label_id (for assign/remove).",
    )
    targets: list[str] = Field(
        default_factory=list,
        description=("Entity IDs or device IDs to assign/remove the label to/from."),
    )
    color: str | None = Field(
        default=None,
        description="Optional hex color for create (e.g. '#ff5733').",
    )


async def handler(params: ManageLabelsParams, ctx: ToolContext) -> ToolResult:
    # "list" is a read — no approval needed. Mutating actions check
    # user_approved explicitly since this tool is registered as tier-1
    # to keep list freely callable.
    if params.action != "list" and not ctx.user_approved:
        return ToolResult.error(
            "confirmation_required",
            f"'{params.action}' requires user approval — click Apply",
        )

    if params.action == "list":
        return ToolResult.ok(
            {
                "labels": [
                    {
                        "label_id": lbl.label_id,
                        "name": lbl.name,
                        "color": lbl.color,
                    }
                    for lbl in ctx.registries.labels.values()
                ]
            }
        )

    if params.action == "create":
        if not params.label:
            return ToolResult.error("missing_param", "create requires 'label' (name)")
        try:
            create_kwargs: dict[str, Any] = {"name": params.label}
            if params.color:
                create_kwargs["color"] = params.color
            result = await ctx.ws_client.send_command(
                "config/label_registry/create", **create_kwargs
            )
        except CommandError as exc:
            return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")
        label_id = result.get("label_id") if isinstance(result, dict) else None
        return ToolResult.ok({"action": "create", "name": params.label, "label_id": label_id})

    if params.action in ("assign", "remove"):
        if not params.label:
            return ToolResult.error("missing_param", f"{params.action} requires 'label' (label_id)")
        if not params.targets:
            return ToolResult.error("missing_param", f"{params.action} requires 'targets'")
        return await _update_labels(
            ctx, params.label, params.targets, add=params.action == "assign"
        )

    return ToolResult.error("invalid_action", f"unknown action {params.action!r}")


async def _update_labels(
    ctx: ToolContext,
    label_id: str,
    targets: list[str],
    *,
    add: bool,
) -> ToolResult:
    results: list[dict[str, Any]] = []
    for target in targets:
        is_entity = "." in target
        try:
            if is_entity:
                entry = ctx.registries.entities.get(target)
                current = set(entry.labels) if entry else set()
                new_labels = sorted(current | {label_id}) if add else sorted(current - {label_id})
                await ctx.ws_client.send_command(
                    "config/entity_registry/update",
                    entity_id=target,
                    labels=new_labels,
                )
            else:
                entry_d = ctx.registries.devices.get(target)
                current = set(entry_d.labels) if entry_d else set()
                new_labels = sorted(current | {label_id}) if add else sorted(current - {label_id})
                await ctx.ws_client.send_command(
                    "config/device_registry/update",
                    device_id=target,
                    labels=new_labels,
                )
            results.append({"target": target, "ok": True})
        except CommandError as exc:
            results.append({"target": target, "ok": False, "error": f"{exc.code}: {exc.message}"})
    all_ok = all(r["ok"] for r in results)
    action_name = "assign" if add else "remove"
    envelope: dict[str, Any] = {
        "action": action_name,
        "label_id": label_id,
        "results": results,
    }
    if not all_ok:
        return ToolResult.error(
            "partial_failure",
            f"some {action_name} operations failed — see results",
            data=envelope,
        )
    return ToolResult.ok(envelope)


TOOL = ToolDefinition(
    name="manage_labels",
    description=(
        "Create, assign, and remove labels/tags on entities, devices, and "
        "automations. 'list' returns all existing labels. Operations take "
        "effect immediately via HA's websocket API. Requires approval for "
        "assign/remove."
    ),
    params_model=ManageLabelsParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
