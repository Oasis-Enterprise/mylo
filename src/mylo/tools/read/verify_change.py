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

"""``verify_change`` — post-change sanity checks.

Implemented: ``entity_exists``, ``automation_loaded``, and
``dashboard_loaded`` (does a saved view actually exist in the fetched
dashboard config, with sane sections shape). The remaining checks
(``no_new_errors``, ``service_available``, ``full_health``) land with
the full rollback loop where they have real consumers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.states import get_all_states
from mylo.ha.ws_client import CommandError
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
        description=(
            "Entity IDs or automation IDs to verify, depending on check_type. "
            "For 'dashboard_loaded': '<dashboard_id>:<view_path>' or a bare "
            "'<view_path>' for the default dashboard."
        ),
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


async def _check_dashboard_loaded(ctx: ToolContext, targets: list[str]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    configs: dict[str | None, dict[str, Any] | None] = {}

    for target in targets:
        dashboard_id, _, view_path = target.rpartition(":")
        dash_key = dashboard_id or None

        if dash_key not in configs:
            try:
                result = await ctx.ws_client.send_command(
                    "lovelace/config", url_path=dash_key
                )
                configs[dash_key] = result if isinstance(result, dict) else None
            except CommandError as exc:
                configs[dash_key] = None
                results[target] = {"ok": False, "reason": f"{exc.code}: {exc.message}"}
                continue

        config = configs[dash_key]
        if config is None:
            results[target] = {"ok": False, "reason": "dashboard config unavailable"}
            continue

        view = next(
            (
                v
                for v in config.get("views") or []
                if isinstance(v, dict) and v.get("path") == view_path
            ),
            None,
        )
        if view is None:
            results[target] = {"ok": False, "reason": "view not found"}
            continue

        is_sections = view.get("type") == "sections" or isinstance(
            view.get("sections"), list
        )
        entry: dict[str, Any] = {
            "ok": True,
            "layout": "sections" if is_sections else "masonry",
        }
        if is_sections:
            sections = view.get("sections")
            broken = not isinstance(sections, list) or any(
                not isinstance(s, dict) or not isinstance(s.get("cards"), list)
                for s in sections
            )
            if broken:
                entry["ok"] = False
                entry["reason"] = "sections view has malformed sections (missing cards list)"
            else:
                entry["section_count"] = len(sections)
        else:
            entry["card_count"] = len(view.get("cards") or [])
        results[target] = entry

    all_ok = bool(results) and all(r.get("ok") for r in results.values())
    return {"check": "dashboard_loaded", "all_ok": all_ok, "results": results}


async def handler(params: VerifyChangeParams, ctx: ToolContext) -> ToolResult:
    if params.wait_seconds > 0:
        await asyncio.sleep(params.wait_seconds)

    if params.check_type == "entity_exists":
        return ToolResult.ok(await _check_entity_exists(ctx, params.targets))
    if params.check_type == "automation_loaded":
        return ToolResult.ok(await _check_automation_loaded(ctx, params.targets))
    if params.check_type == "dashboard_loaded":
        return ToolResult.ok(await _check_dashboard_loaded(ctx, params.targets))

    return ToolResult.error(
        "not_implemented",
        f"check_type {params.check_type!r} lands with the write tools in M7",
    )


TOOL = ToolDefinition(
    name="verify_change",
    description=(
        "After a config change and reload, verify the change took effect. "
        "Implements 'entity_exists', 'automation_loaded', and "
        "'dashboard_loaded' (targets: '<dashboard_id>:<view_path>' or bare "
        "'<view_path>' for the default dashboard — checks the view exists "
        "and its sections are well-formed). Richer verifications land in a "
        "later milestone."
    ),
    params_model=VerifyChangeParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
