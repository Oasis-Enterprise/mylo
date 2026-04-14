"""``verify_change`` — post-change sanity checks.

Scope for M2 is minimal: check whether entities exist (``entity_exists``) and
whether an automation entity is loaded and enabled (``automation_loaded``).
The richer checks (``dashboard_loaded``, ``no_new_errors``,
``service_available``, ``full_health``) land with the full rollback loop in
M7 where they have real consumers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.states import get_all_states
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

CheckType = Literal[
    "entity_exists",
    "automation_loaded",
    "dashboard_loaded",
    "no_new_errors",
    "service_available",
    "full_health",
]


class VerifyChangeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_type: CheckType = Field(description="Which verification to perform.")
    targets: list[str] = Field(
        default_factory=list,
        description="Entity IDs or automation IDs to verify, depending on check_type.",
    )
    wait_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=30.0,
        description="Grace period before checking (lets HA finish a reload).",
    )


async def _check_entity_exists(ctx: ToolContext, targets: list[str]) -> dict[str, Any]:
    # Refresh registry first: a freshly-created entity may arrive via a
    # registry_updated event that the live handler already processed, but a
    # quick refresh removes any remaining race.
    await ctx.registries.refresh()
    present = {eid: eid in ctx.registries.entities for eid in targets}
    return {
        "check": "entity_exists",
        "all_ok": all(present.values()) if present else True,
        "results": present,
    }


async def _check_automation_loaded(ctx: ToolContext, targets: list[str]) -> dict[str, Any]:
    await ctx.registries.refresh()
    states = await get_all_states(ctx.ws_client)
    results: dict[str, Any] = {}
    for entity_id in targets:
        if not entity_id.startswith("automation."):
            results[entity_id] = {"ok": False, "reason": "not an automation entity"}
            continue
        in_registry = entity_id in ctx.registries.entities
        state = states.get(entity_id)
        current = state.get("state") if state else None
        results[entity_id] = {
            "ok": in_registry and current in ("on", "off"),
            "in_registry": in_registry,
            "state": current,
        }
    all_ok = bool(results) and all(r["ok"] for r in results.values())
    return {"check": "automation_loaded", "all_ok": all_ok, "results": results}


async def handler(params: VerifyChangeParams, ctx: ToolContext) -> ToolResult:
    if params.wait_seconds > 0:
        await asyncio.sleep(params.wait_seconds)

    if params.check_type == "entity_exists":
        return ToolResult.ok(await _check_entity_exists(ctx, params.targets))
    if params.check_type == "automation_loaded":
        return ToolResult.ok(await _check_automation_loaded(ctx, params.targets))

    return ToolResult.error(
        "not_implemented",
        f"check_type {params.check_type!r} lands with the write tools in M7",
    )


TOOL = ToolDefinition(
    name="verify_change",
    description=(
        "After a config change and reload, verify the change took effect. "
        "M2 implements 'entity_exists' and 'automation_loaded'; richer "
        "verifications are wired up alongside the write tools in a later "
        "milestone."
    ),
    params_model=VerifyChangeParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
