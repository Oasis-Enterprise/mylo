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

"""``query_entities`` — filter HA entities by area/domain/pattern/device_class
and return shaped output with optional state + attributes.

This is the single most-used tool, so the shape of its output sets the tone
for everything else. See spec §4.3 and §4.10.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.registries import EntityEntry
from mylo.logging_setup import get_logger
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.formatters import shape_entity, shape_entity_minimal, summarize_entities
from mylo.tools.registry import register

log = get_logger(__name__)

Detail = Literal["minimal", "standard", "full"]


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area: str | None = Field(
        default=None,
        description="Area id or area name (case-insensitive).",
    )
    domain: str | None = Field(
        default=None,
        description="Entity domain, e.g. 'light', 'sensor', 'climate'.",
    )
    device_class: str | None = Field(
        default=None,
        description="HA device_class attribute, e.g. 'temperature', 'motion'.",
    )
    integration: str | None = Field(
        default=None,
        description="Platform / integration name, e.g. 'hue', 'esphome', 'mqtt'.",
    )
    pattern: str | None = Field(
        default=None,
        description=(
            "Regex (Python syntax) matched against both entity_id and friendly_name. "
            "Case-insensitive."
        ),
    )
    state: str | None = Field(
        default=None,
        description="Only include entities whose current state equals this string.",
    )


class QueryEntitiesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: Filter = Field(default_factory=Filter)
    detail: Detail = Field(
        default="minimal",
        description=(
            "Response depth. 'minimal': entity_id, friendly_name, state, area "
            "(~30 tokens each — use for broad queries like 'what lights are on'). "
            "'standard': + key attributes (brightness, temperature, etc). "
            "'full': + all attributes, device info, integration (~150 tokens each — "
            "only for targeted queries on a few entities)."
        ),
    )
    include_disabled: bool = Field(
        default=False,
        description="Include entities disabled by HA or integration.",
    )
    include_hidden: bool = Field(
        default=False,
        description="Include user-hidden entities.",
    )
    limit: int = Field(
        default=200,
        ge=1,
        le=2000,
        description=(
            "Max entities to return. Default 200. For a bulk gather (e.g. "
            "building a dashboard) raise to 2000 to get everything in ONE "
            "call rather than many small ones."
        ),
    )


# ─── Matching helpers ────────────────────────────────────────────────────────


def _resolve_area_id(ctx: ToolContext, area_query: str) -> str | None:
    """Return the matching ``area_id`` given an id or (case-insensitive) name."""
    if area_query in ctx.registries.areas:
        return area_query
    lower = area_query.lower()
    for a in ctx.registries.areas.values():
        if a.name.lower() == lower:
            return a.area_id
    return None


def _effective_area_id(ctx: ToolContext, entry: EntityEntry) -> str | None:
    if entry.area_id:
        return entry.area_id
    if entry.device_id:
        dev = ctx.registries.devices.get(entry.device_id)
        if dev and dev.area_id:
            return dev.area_id
    return None


def _match(
    ctx: ToolContext,
    entry: EntityEntry,
    state: dict[str, Any] | None,
    filt: Filter,
    target_area_id: str | None,
    pattern: re.Pattern[str] | None,
) -> bool:
    if filt.area and _effective_area_id(ctx, entry) != target_area_id:
        return False
    if filt.domain and entry.domain != filt.domain:
        return False
    if filt.integration and (entry.platform or "") != filt.integration:
        return False
    if filt.state is not None and (not state or state.get("state") != filt.state):
        return False
    if filt.device_class is not None:
        dc = (state or {}).get("attributes", {}).get("device_class")
        if dc != filt.device_class:
            return False
    return not (
        pattern is not None
        and not (pattern.search(entry.entity_id) or pattern.search(entry.friendly_name))
    )


# ─── Handler ─────────────────────────────────────────────────────────────────


async def handler(params: QueryEntitiesParams, ctx: ToolContext) -> ToolResult:
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

    # Fetch state once (cached for the duration of a turn); we filter
    # locally from there.
    try:
        states = await ctx.states.get(ctx.ws_client)
    except Exception as exc:
        return ToolResult.error("ha_unavailable", f"get_states failed: {exc}")

    matched: list[EntityEntry] = []
    for entry in ctx.registries.entities.values():
        if not params.include_disabled and entry.disabled_by:
            continue
        if not params.include_hidden and entry.hidden_by:
            continue
        state = states.get(entry.entity_id)
        if _match(ctx, entry, state, filt, target_area_id, pattern):
            matched.append(entry)

    truncated = len(matched) > params.limit
    entries = matched[: params.limit]

    # Hard cap: returning standard/full detail for >100 entities would be
    # 10K-15K tokens. Downgrade to minimal based on how many rows we'd
    # actually emit (not the limit param) so detail=full still works on a
    # narrow filter even when the default limit is high.
    detail = params.detail
    if detail != "minimal" and len(entries) > 100:
        detail = "minimal"
        log.info(
            "query_entities.detail_downgraded",
            reason=">100 entities with non-minimal detail",
            returned=len(entries),
        )

    if detail == "minimal":
        shaped = [shape_entity_minimal(e, states.get(e.entity_id), ctx.registries) for e in entries]
    else:
        shaped = [
            shape_entity(
                e,
                states.get(e.entity_id),
                ctx.registries,
                include_attributes=detail == "full",
            )
            for e in entries
        ]

    envelope = summarize_entities(shaped, area_name=area_name)
    envelope["total_before_limit"] = len(matched)
    if truncated:
        envelope["truncated"] = True
    return ToolResult.ok(envelope)


# ─── Registration ────────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="query_entities",
    description=(
        "Search entities. Match the filter to the JOB: for a narrow question "
        "('what lights are on') use the narrowest filter (domain=light + "
        "state=on). But for a BULK gather (building a dashboard, auditing a "
        "whole area/home) do ONE broad query, not many small ones — omit "
        "domain to get every entity in an area, or omit all filters with "
        "limit=2000 to get the whole home. detail=minimal (the default) "
        "already returns the exact entity_id for every result, so a single "
        "broad minimal query gives you all the ids you need; don't re-query "
        "per-domain. Use detail=standard/full only for attributes on a few "
        "entities. Check the topology summary first — it may already answer "
        "the question without a tool call."
    ),
    params_model=QueryEntitiesParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
