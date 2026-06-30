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

"""``manage_helpers`` — create, update, and delete HA helper entities.

Helpers are HA's built-in virtual entities: input_boolean (toggles),
input_number (sliders), input_select (dropdowns), input_text (text
fields), input_datetime (date/time pickers), timer, counter,
input_button (momentary buttons), and schedule (weekly time blocks).
Users currently have to create these one at a time through
Settings → Helpers in the UI.

All operations use HA's websocket API and take effect immediately —
no file writes, no reload. The ``list`` action is tier-1 (free);
``create``, ``update``, and ``delete`` require user approval.

Websocket commands per helper type:
  input_boolean  — config/input_boolean/{create,update,delete}
  input_number   — config/input_number/{create,update,delete}
  input_select   — config/input_select/{create,update,delete}
  input_text     — config/input_text/{create,update,delete}
  input_datetime — config/input_datetime/{create,update,delete}
  timer          — config/timer/{create,update,delete}
  counter        — config/counter/{create,update,delete}
  input_button   — config/input_button/{create,update,delete}
  schedule       — config/schedule/{create,update,delete}
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError
from mylo.logging_setup import get_logger
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)

Action = Literal["create", "update", "delete", "list"]
HelperType = Literal[
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
    "timer",
    "counter",
    "input_button",
    "schedule",
]


class ManageHelpersParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(
        description=(
            "'list' shows existing helpers of a type. "
            "'create' a new helper. 'update' an existing one. "
            "'delete' removes a helper by id."
        ),
    )
    helper_type: HelperType = Field(
        description="The type of helper entity to manage.",
    )
    helper_id: str | None = Field(
        default=None,
        description=(
            "For update/delete: the helper's id (without the domain prefix). "
            "E.g. for input_boolean.guest_mode, pass 'guest_mode'."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Display name for create/update. E.g. 'Guest Mode'.",
    )
    icon: str | None = Field(
        default=None,
        description="MDI icon for create/update. E.g. 'mdi:account-group'.",
    )
    # Type-specific config fields. Each helper type uses a subset.
    initial_value: str | float | bool | None = Field(
        default=None,
        description=(
            "Initial/default value. For input_boolean: true/false. "
            "For input_number: a number. For counter: initial count. "
            "For timer: duration as 'HH:MM:SS'."
        ),
    )
    min_value: float | None = Field(
        default=None,
        description="For input_number: minimum value.",
    )
    max_value: float | None = Field(
        default=None,
        description="For input_number: maximum value.",
    )
    step: float | None = Field(
        default=None,
        description="For input_number: step size.",
    )
    unit_of_measurement: str | None = Field(
        default=None,
        description="For input_number: unit string (e.g. '°F', '%').",
    )
    mode: str | None = Field(
        default=None,
        description="For input_number: 'slider' or 'box'. For input_text: 'text' or 'password'.",
    )
    options: list[str] | None = Field(
        default=None,
        description="For input_select: list of selectable options.",
    )
    min_len: int | None = Field(
        default=None,
        description="For input_text: minimum length.",
    )
    max_len: int | None = Field(
        default=None,
        description="For input_text: maximum length (default 100).",
    )
    has_date: bool | None = Field(
        default=None,
        description="For input_datetime: include date picker.",
    )
    has_time: bool | None = Field(
        default=None,
        description="For input_datetime: include time picker.",
    )
    duration: str | None = Field(
        default=None,
        description="For timer: duration as 'HH:MM:SS'.",
    )
    restore: bool | None = Field(
        default=None,
        description="For timer: restore state after HA restart.",
    )
    minimum: int | None = Field(
        default=None,
        description="For counter: minimum value.",
    )
    maximum: int | None = Field(
        default=None,
        description="For counter: maximum value.",
    )
    schedule_blocks: dict[str, list[dict[str, str]]] | None = Field(
        default=None,
        description=(
            "For schedule: weekday -> list of {from, to} time segments, e.g. "
            "{'monday': [{'from': '08:00:00', 'to': '17:00:00'}]}. Keys: monday..sunday."
        ),
    )


async def handler(params: ManageHelpersParams, ctx: ToolContext) -> ToolResult:
    if params.action == "list":
        return await _list_helpers(ctx, params.helper_type)

    if not ctx.user_approved:
        return ToolResult.error(
            "confirmation_required",
            f"'{params.action}' requires user approval — click Apply",
        )

    if params.action == "create":
        return await _create_helper(ctx, params)
    if params.action == "update":
        return await _update_helper(ctx, params)
    if params.action == "delete":
        return await _delete_helper(ctx, params)

    return ToolResult.error("invalid_action", f"unknown action {params.action!r}")


async def _list_helpers(ctx: ToolContext, helper_type: HelperType) -> ToolResult:
    """List all helpers of a given type from the registry."""
    domain = helper_type
    matched = [
        {
            "entity_id": e.entity_id,
            "name": e.friendly_name,
            "platform": e.platform,
        }
        for e in ctx.registries.entities.values()
        if e.domain == domain and not e.disabled_by
    ]
    return ToolResult.ok(
        {
            "helper_type": helper_type,
            "count": len(matched),
            "helpers": matched,
        }
    )


async def _create_helper(ctx: ToolContext, params: ManageHelpersParams) -> ToolResult:
    if not params.name:
        return ToolResult.error("missing_param", "create requires 'name'")

    cmd = f"config/{params.helper_type}/create"
    payload = _build_payload(params)

    try:
        result = await ctx.ws_client.send_command(cmd, write=True, **payload)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    helper_id = None
    if isinstance(result, dict):
        helper_id = result.get("id")

    return ToolResult.ok(
        {
            "action": "create",
            "helper_type": params.helper_type,
            "name": params.name,
            "id": helper_id,
        }
    )


async def _update_helper(ctx: ToolContext, params: ManageHelpersParams) -> ToolResult:
    if not params.helper_id:
        return ToolResult.error("missing_param", "update requires 'helper_id'")

    cmd = f"config/{params.helper_type}/update"
    payload = _build_payload(params)
    payload[f"{params.helper_type}_id"] = params.helper_id

    try:
        await ctx.ws_client.send_command(cmd, write=True, **payload)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok(
        {
            "action": "update",
            "helper_type": params.helper_type,
            "id": params.helper_id,
        }
    )


async def _delete_helper(ctx: ToolContext, params: ManageHelpersParams) -> ToolResult:
    if not params.helper_id:
        return ToolResult.error("missing_param", "delete requires 'helper_id'")

    cmd = f"config/{params.helper_type}/delete"
    delete_payload: dict[str, Any] = {f"{params.helper_type}_id": params.helper_id}

    try:
        await ctx.ws_client.send_command(cmd, write=True, **delete_payload)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok(
        {
            "action": "delete",
            "helper_type": params.helper_type,
            "id": params.helper_id,
        }
    )


def _build_payload(params: ManageHelpersParams) -> dict[str, Any]:
    """Build the websocket command payload from the params, including
    only the fields relevant to the helper type."""
    payload: dict[str, Any] = {}

    if params.name is not None:
        payload["name"] = params.name
    if params.icon is not None:
        payload["icon"] = params.icon

    ht = params.helper_type

    if ht == "input_boolean":
        if params.initial_value is not None:
            payload["initial"] = bool(params.initial_value)

    elif ht == "input_number":
        if params.min_value is not None:
            payload["min"] = params.min_value
        if params.max_value is not None:
            payload["max"] = params.max_value
        if params.step is not None:
            payload["step"] = params.step
        if params.unit_of_measurement is not None:
            payload["unit_of_measurement"] = params.unit_of_measurement
        if params.mode is not None:
            payload["mode"] = params.mode
        if params.initial_value is not None:
            payload["initial"] = float(params.initial_value)

    elif ht == "input_select":
        if params.options is not None:
            payload["options"] = params.options
        if params.initial_value is not None:
            payload["initial"] = str(params.initial_value)

    elif ht == "input_text":
        if params.min_len is not None:
            payload["min"] = params.min_len
        if params.max_len is not None:
            payload["max"] = params.max_len
        if params.mode is not None:
            payload["mode"] = params.mode
        if params.initial_value is not None:
            payload["initial"] = str(params.initial_value)

    elif ht == "input_datetime":
        if params.has_date is not None:
            payload["has_date"] = params.has_date
        if params.has_time is not None:
            payload["has_time"] = params.has_time
        if params.initial_value is not None:
            payload["initial"] = str(params.initial_value)

    elif ht == "timer":
        if params.duration is not None:
            payload["duration"] = params.duration
        if params.restore is not None:
            payload["restore"] = params.restore

    elif ht == "counter":
        if params.initial_value is not None:
            payload["initial"] = int(params.initial_value)
        if params.minimum is not None:
            payload["minimum"] = params.minimum
        if params.maximum is not None:
            payload["maximum"] = params.maximum
        if params.step is not None:
            payload["step"] = int(params.step)

    elif ht == "input_button":
        pass  # only name/icon — already added above; no extra fields

    elif ht == "schedule" and params.schedule_blocks:
        for day, segments in params.schedule_blocks.items():
            payload[day] = segments

    return payload


TOOL = ToolDefinition(
    name="manage_helpers",
    description=(
        "Create, update, delete, or list Home Assistant helper entities: "
        "input_boolean (toggles), input_number (sliders), input_select "
        "(dropdowns), input_text, input_datetime, timer, counter, "
        "input_button (momentary buttons), and schedule (weekly time blocks). "
        "Use 'list' to see existing helpers of a type. Create/update/delete "
        "require approval. All operations are immediate via websocket — "
        "no file writes or reload needed."
    ),
    params_model=ManageHelpersParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
