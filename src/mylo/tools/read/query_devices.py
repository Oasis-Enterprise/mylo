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

"""``query_devices`` — retrieve device-registry information.

Spec §4.3. Device queries are where area/manufacturer/model slicing happens;
entity queries are too low-level for device-centric questions ("what
thermostats do I have?").
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.registries import DeviceEntry, EntityEntry
from mylo.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from mylo.tools.context import ToolContext
from mylo.tools.formatters import shape_device
from mylo.tools.registry import register


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area: str | None = Field(default=None, description="Area id or name (case-insensitive).")
    manufacturer: str | None = Field(
        default=None, description="Manufacturer string match (case-insensitive)."
    )
    model: str | None = Field(default=None, description="Model string match (case-insensitive).")
    integration: str | None = Field(
        default=None,
        description=(
            "Filter devices that have at least one entity from this integration/"
            "platform (e.g. 'hue', 'shelly', 'zha')."
        ),
    )
    pattern: str | None = Field(
        default=None,
        description="Regex matched against device name / manufacturer / model.",
    )


class QueryDevicesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: Filter = Field(default_factory=Filter)
    include_entities: bool = Field(
        default=False,
        description=(
            "Include the list of entities belonging to each device. Off by "
            "default — this can balloon results on device-rich setups."
        ),
    )
    include_disabled: bool = Field(
        default=False,
        description="Include devices disabled by user or integration.",
    )
    limit: int = Field(default=200, ge=1, le=2000)


def _resolve_area_id(ctx: ToolContext, area_query: str) -> str | None:
    if area_query in ctx.registries.areas:
        return area_query
    lower = area_query.lower()
    for a in ctx.registries.areas.values():
        if a.name.lower() == lower:
            return a.area_id
    return None


def _device_integrations(ctx: ToolContext, device: DeviceEntry) -> set[str]:
    return {
        e.platform
        for e in ctx.registries.entities.values()
        if e.device_id == device.id and e.platform
    }


def _matches(
    ctx: ToolContext,
    device: DeviceEntry,
    filt: Filter,
    target_area_id: str | None,
    pattern: re.Pattern[str] | None,
) -> bool:
    if filt.area and device.area_id != target_area_id:
        return False
    if filt.manufacturer and (
        not device.manufacturer or filt.manufacturer.lower() not in device.manufacturer.lower()
    ):
        return False
    if filt.model and (not device.model or filt.model.lower() not in device.model.lower()):
        return False
    if filt.integration and filt.integration not in _device_integrations(ctx, device):
        return False
    if pattern is not None:
        haystack = " ".join(
            s for s in (device.display_name, device.manufacturer, device.model) if s
        )
        if not pattern.search(haystack):
            return False
    return True


async def handler(params: QueryDevicesParams, ctx: ToolContext) -> ToolResult:
    filt = params.filter

    target_area_id: str | None = None
    area_name: str | None = None
    if filt.area:
        target_area_id = _resolve_area_id(ctx, filt.area)
        if target_area_id is None:
            known = sorted(a.name for a in ctx.registries.areas.values())
            return ToolResult.error(
                "area_not_found",
                f"no area matching {filt.area!r}",
                data={"did_you_mean": known},
            )
        area_name = ctx.registries.areas[target_area_id].name

    pattern: re.Pattern[str] | None = None
    if filt.pattern:
        try:
            pattern = re.compile(filt.pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult.error("invalid_regex", str(exc))

    matched: list[DeviceEntry] = []
    for device in ctx.registries.devices.values():
        if not params.include_disabled and device.disabled_by:
            continue
        if _matches(ctx, device, filt, target_area_id, pattern):
            matched.append(device)

    # Uniform row bounding: keep the head, report the true total + a hint.
    bounded = bound_rows(
        matched,
        max_rows=params.limit,
        hint="Narrow with area, integration, or name filters to see the rest.",
    )
    shown = bounded["rows"]

    shaped = [
        shape_device(d, ctx.registries, include_entities=params.include_entities) for d in shown
    ]

    envelope: dict[str, Any] = {
        "devices_found": bounded["total"],
        "devices": shaped,
    }
    if area_name is not None:
        envelope["area"] = area_name
    if bounded.get("truncated"):
        envelope["truncated"] = True
        envelope["hint"] = bounded["hint"]
    # Manufacturer summary — cheap insight for queries without a pattern.
    by_manufacturer: dict[str, int] = {}
    for d in shown:
        key = d.manufacturer or "unknown"
        by_manufacturer[key] = by_manufacturer.get(key, 0) + 1
    envelope["by_manufacturer"] = dict(sorted(by_manufacturer.items(), key=lambda kv: -kv[1]))

    return ToolResult.ok(envelope)


TOOL = ToolDefinition(
    name="query_devices",
    description=(
        "Retrieve device registry information: manufacturer, model, area, "
        "and associated entities. Filter by area, manufacturer, model, "
        "integration, or regex pattern across name/manufacturer/model."
    ),
    params_model=QueryDevicesParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)


__all__ = ["TOOL", "EntityEntry"]  # re-export kept for easy test discovery
