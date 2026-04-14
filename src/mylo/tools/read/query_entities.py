"""``query_entities`` — filter HA entities by area/domain/pattern/device_class
and return shaped output with optional state + attributes.

This is the single most-used tool, so the shape of its output sets the tone
for everything else. See spec §4.3 and §4.10.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.registries import EntityEntry
from mylo.ha.states import get_all_states
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.formatters import shape_entity, summarize_entities
from mylo.tools.registry import register


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
    include_attributes: bool = Field(
        default=False,
        description=(
            "Include domain-relevant attributes on each entity (brightness as "
            "percent, climate current temp, etc)."
        ),
    )
    limit: int = Field(
        default=200,
        ge=1,
        le=2000,
        description=(
            "Maximum number of entities to return. Raise deliberately; default "
            "handles typical homes."
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

    # Fetch state once; we filter locally from there.
    try:
        states = await get_all_states(ctx.ws_client)
    except Exception as exc:
        return ToolResult.error("ha_unavailable", f"get_states failed: {exc}")

    matched: list[EntityEntry] = []
    for entry in ctx.registries.entities.values():
        state = states.get(entry.entity_id)
        if _match(ctx, entry, state, filt, target_area_id, pattern):
            matched.append(entry)

    truncated = len(matched) > params.limit
    entries = matched[: params.limit]

    shaped = [
        shape_entity(
            e,
            states.get(e.entity_id),
            ctx.registries,
            include_attributes=params.include_attributes,
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
        "Search and retrieve entity states, attributes, and metadata. "
        "Supports filtering by area, domain, device_class, integration, or a "
        "regex pattern on entity_id and friendly_name. Returns a compact "
        "summary plus the matching entities."
    ),
    params_model=QueryEntitiesParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
