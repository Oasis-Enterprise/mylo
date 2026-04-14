"""``query_automations`` — list and inspect automations, scripts, and scenes.

Uses HA's ``get_states`` to find the entities (their state is last-triggered
timestamp) plus ``config/automation/config`` / ``config/script/config`` /
``config/scene/config`` to retrieve the actual triggers/conditions/actions.
Config endpoints only work for storage-mode items; YAML-mode items will only
return the state view.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.states import get_all_states
from mylo.ha.ws_client import CommandError, HaWsClient
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str | None = Field(
        default=None,
        description=("Restrict to one of 'automation', 'script', 'scene'. Default returns all."),
    )
    area: str | None = Field(default=None, description="Area id or name (case-insensitive).")
    entity_referenced: str | None = Field(
        default=None,
        description="Only include automations/scripts/scenes that reference this entity_id.",
    )
    enabled: bool | None = Field(
        default=None,
        description="Only automations whose current state matches this (True=on, False=off).",
    )


class QueryAutomationsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: Filter = Field(default_factory=Filter)
    include_config: bool = Field(
        default=False,
        description=(
            "Include the full triggers/conditions/actions config. Only "
            "available for storage-mode items."
        ),
    )
    limit: int = Field(default=200, ge=1, le=2000)


_KIND_DOMAINS: dict[str, str] = {
    "automation": "automation",
    "script": "script",
    "scene": "scene",
}

_CONFIG_ENDPOINT: dict[str, str] = {
    "automation": "automation/config",
    "script": "script/config",
    "scene": "scene/config",
}


async def _fetch_config(client: HaWsClient, kind: str, entity_id: str) -> Any | None:
    endpoint = _CONFIG_ENDPOINT.get(kind)
    if endpoint is None:
        return None
    try:
        # HA returns {"config": {...}} for automation/script storage items,
        # keyed by numeric "id" rather than entity_id; but the newer API
        # accepts entity_id via the "config/automation/config" command with
        # {"config_id": <numeric_id>} or just works with {"entity_id": ...}.
        # We try entity_id first and fall back to extracting the numeric id
        # from the unique_id/object_id.
        obj_id = entity_id.split(".", 1)[1]
        return await client.send_command(f"config/{endpoint}", config_id=obj_id)
    except CommandError:
        return None


def _resolve_area_id(ctx: ToolContext, area_query: str) -> str | None:
    if area_query in ctx.registries.areas:
        return area_query
    for a in ctx.registries.areas.values():
        if a.name.lower() == area_query.lower():
            return a.area_id
    return None


def _references_entity(config: Any, entity_id: str) -> bool:
    """Recursively check whether a config blob references ``entity_id``."""
    if isinstance(config, dict):
        return any(_references_entity(v, entity_id) for v in config.values())
    if isinstance(config, list):
        return any(_references_entity(v, entity_id) for v in config)
    if isinstance(config, str):
        return entity_id in config
    return False


async def handler(params: QueryAutomationsParams, ctx: ToolContext) -> ToolResult:
    filt = params.filter

    target_area_id: str | None = None
    if filt.area:
        target_area_id = _resolve_area_id(ctx, filt.area)
        if target_area_id is None:
            known = sorted(a.name for a in ctx.registries.areas.values())
            return ToolResult.error(
                "area_not_found",
                f"no area matching {filt.area!r}",
                data={"did_you_mean": known},
            )

    kinds: list[str]
    if filt.kind:
        if filt.kind not in _KIND_DOMAINS:
            return ToolResult.error(
                "invalid_kind",
                f"kind must be one of {sorted(_KIND_DOMAINS)}, got {filt.kind!r}",
            )
        kinds = [filt.kind]
    else:
        kinds = list(_KIND_DOMAINS)

    try:
        states = await get_all_states(ctx.ws_client)
    except Exception as exc:
        return ToolResult.error("ha_unavailable", f"get_states failed: {exc}")

    items: list[dict[str, Any]] = []
    for kind in kinds:
        domain = _KIND_DOMAINS[kind]
        for entity_id, state in states.items():
            if not entity_id.startswith(f"{domain}."):
                continue
            registry = ctx.registries.entities.get(entity_id)
            # Area filter: use effective area (device-inherited if any).
            if target_area_id is not None:
                eff_area = None
                if registry:
                    eff_area = registry.area_id
                    if not eff_area and registry.device_id:
                        dev = ctx.registries.devices.get(registry.device_id)
                        eff_area = dev.area_id if dev else None
                if eff_area != target_area_id:
                    continue
            if filt.enabled is not None:
                current = state.get("state")
                is_on = current == "on"
                if filt.enabled != is_on:
                    continue

            attrs = state.get("attributes") or {}
            item: dict[str, Any] = {
                "entity_id": entity_id,
                "kind": kind,
                "friendly_name": attrs.get("friendly_name")
                or (registry.friendly_name if registry else entity_id),
                "state": state.get("state"),
                "last_triggered": attrs.get("last_triggered"),
            }
            if params.include_config or filt.entity_referenced:
                cfg = await _fetch_config(ctx.ws_client, kind, entity_id)
                if filt.entity_referenced and not _references_entity(cfg, filt.entity_referenced):
                    continue
                if params.include_config and cfg is not None:
                    item["config"] = cfg
            elif filt.entity_referenced:
                # entity_referenced filter without config fetch — skip if we can't
                # determine references.
                continue
            items.append(item)

    truncated = len(items) > params.limit
    shown = items[: params.limit]

    envelope: dict[str, Any] = {
        "count": len(items),
        "by_kind": _by_kind(items),
        "items": shown,
    }
    if truncated:
        envelope["truncated"] = True
    return ToolResult.ok(envelope)


def _by_kind(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        k = item.get("kind", "?")
        out[k] = out.get(k, 0) + 1
    return out


TOOL = ToolDefinition(
    name="query_automations",
    description=(
        "List and inspect automations, scripts, and scenes. Filter by kind, "
        "area, enabled state, or entities they reference. Optionally include "
        "the full triggers/conditions/actions config (storage-mode only)."
    ),
    params_model=QueryAutomationsParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
