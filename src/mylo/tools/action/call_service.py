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

"""``call_service`` — tier-3 tool that invokes any HA service.

Spec §4.5. This is how Mylo actually turns lights on, locks doors,
triggers scripts, etc. Every call consumes a tier-3 rate-limit token
(see :mod:`mylo.safety.permissions`) and requires ``user_approved=True``
on the context.

The blocklist here is hard: these services are never callable, no
matter what. The restricted list produces a warning included in the
envelope so the UI can surface extra-explicit language ("You're about
to unlock a lock") before the user hits Apply.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError, CommandTimeout
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

# Hard-blocked — these can only land through manual HA intervention.
BLOCKED_SERVICES: frozenset[str] = frozenset(
    {
        "homeassistant/restart",
        "homeassistant/stop",
        "hassio/host_reboot",
        "hassio/host_shutdown",
        "hassio/supervisor_reload",
    }
)

# Not blocked but warrants extra confirmation in the dry-run preview.
RESTRICTED_SERVICES: dict[str, str] = {
    "lock/unlock": "You're about to unlock a lock",
    "alarm_control_panel/alarm_disarm": "You're about to disarm the alarm",
    "cover/open_cover": "You're about to open a cover (garage door / blind)",
    "cover/open_cover_tilt": "You're about to tilt a cover open",
}


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: list[str] | str | None = None
    area_id: list[str] | str | None = None
    device_id: list[str] | str | None = None


class CallServiceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(description="Service domain, e.g. 'light', 'lock', 'script'.")
    service: str = Field(description="Service name within the domain, e.g. 'turn_on'.")
    target: Target = Field(default_factory=Target)
    data: dict[str, Any] = Field(default_factory=dict)


def _service_key(domain: str, service: str) -> str:
    return f"{domain}/{service}"


def _target_payload(target: Target) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if target.entity_id is not None:
        payload["entity_id"] = target.entity_id
    if target.area_id is not None:
        payload["area_id"] = target.area_id
    if target.device_id is not None:
        payload["device_id"] = target.device_id
    return payload


async def handler(params: CallServiceParams, ctx: ToolContext) -> ToolResult:
    key = _service_key(params.domain, params.service)
    if key in BLOCKED_SERVICES:
        return ToolResult.error(
            "service_blocked",
            f"service {key!r} is blocked — cannot be called through Mylo",
            data={"service": key},
        )

    warning = RESTRICTED_SERVICES.get(key)
    target = _target_payload(params.target)

    # Only include target/service_data keys when non-empty. HA's websocket
    # rejects service_data=null with "invalid_format" and some services
    # choke on an empty target={}.
    payload: dict[str, Any] = {
        "domain": params.domain,
        "service": params.service,
    }
    if target:
        payload["target"] = target
    if params.data:
        payload["service_data"] = params.data

    try:
        await ctx.ws_client.send_command("call_service", **payload)
    except CommandTimeout as exc:
        return ToolResult.error("ha_timeout", str(exc))
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")
    except Exception as exc:
        return ToolResult.error("ha_unavailable", f"{type(exc).__name__}: {exc}")

    envelope: dict[str, Any] = {
        "service": key,
        "target": target,
        "data": params.data,
    }
    if warning:
        envelope["warning"] = warning
    return ToolResult.ok(envelope)


TOOL = ToolDefinition(
    name="call_service",
    description=(
        "Execute a Home Assistant service — turn on/off devices, set values, "
        "trigger scenes, run scripts. Requires explicit user approval. "
        "Certain services are hard-blocked (host restart/shutdown). Others "
        "are flagged with a warning (unlock a lock, disarm alarm, open a "
        "cover). Rate-limited."
    ),
    params_model=CallServiceParams,
    tier=Tier.ACTION,
    handler=handler,
)
register(TOOL)
