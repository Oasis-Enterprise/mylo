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

"""``modify_zones`` — create, update, or delete HA zones.

Zones define geographic areas (home, work, school, etc.) that Home Assistant
uses for presence detection, automations, and device tracking. Agent-managed
zones live alongside automations in ``/config/packages/agent.yaml`` under a
``zone:`` list key:

    zone:
      - id: "work"
        name: "Work"
        latitude: 37.4
        longitude: -122.1
        radius: 100
        icon: "mdi:briefcase"
        passive: false

The built-in ``home`` zone comes from core HA configuration and must not be
edited or deleted here — attempts to target it are rejected with a clear error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from slugify import slugify

from mylo.files.diff import diff_structs
from mylo.files.manager import exists, read_text
from mylo.files.rollback import apply_optimistic_reload_all
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register
from mylo.validators.yaml_parser import dump_yaml, load_yaml

Action = Literal["create", "update", "delete"]

AGENT_PACKAGE_PATH = "packages/agent.yaml"

_HOME_ZONE_ID = "home"


class ModifyZonesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(description="create | update | delete")
    zone_id: str | None = Field(
        default=None,
        description=(
            "Zone identifier matching the zone's `id:` field. Required for "
            "update/delete. For create, derived from name if omitted."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Human-readable zone name. Required for create.",
    )
    latitude: float | None = Field(
        default=None,
        description="Latitude of the zone center in decimal degrees. Required for create.",
    )
    longitude: float | None = Field(
        default=None,
        description="Longitude of the zone center in decimal degrees. Required for create.",
    )
    radius: float | None = Field(
        default=None,
        description=("Radius of the zone in meters. Defaults to 100 on create if not provided."),
    )
    icon: str | None = Field(
        default=None,
        description="MDI icon for the zone, e.g. 'mdi:briefcase'.",
    )
    passive: bool | None = Field(
        default=None,
        description=(
            "If true, the zone is only used for automation triggers, not for "
            "setting device_tracker state."
        ),
    )
    dry_run: bool = Field(default=True)


async def handler(params: ModifyZonesParams, ctx: ToolContext) -> ToolResult:
    # ── home zone guard ────────────────────────────────────────────────────────
    effective_id = params.zone_id or ""
    effective_name = params.name or ""
    if effective_id.lower() == _HOME_ZONE_ID or effective_name.lower() == _HOME_ZONE_ID:
        return ToolResult.error(
            "forbidden",
            (
                "The built-in 'home' zone is managed by Home Assistant core and "
                "cannot be created, updated, or deleted via this tool."
            ),
        )

    # ── param validation ───────────────────────────────────────────────────────
    if params.action in ("update", "delete") and not params.zone_id:
        return ToolResult.error(
            "missing_param",
            f"action {params.action!r} requires 'zone_id'",
        )

    if params.action == "create":
        if not params.name:
            return ToolResult.error(
                "missing_param",
                "action 'create' requires 'name'",
            )
        if params.latitude is None:
            return ToolResult.error(
                "missing_param",
                "action 'create' requires 'latitude'",
            )
        if params.longitude is None:
            return ToolResult.error(
                "missing_param",
                "action 'create' requires 'longitude'",
            )

    # ── load package ───────────────────────────────────────────────────────────
    pkg_path = ctx.config.ha_config_dir / AGENT_PACKAGE_PATH
    current_pkg = _load_package(pkg_path)
    zones: list[dict[str, Any]] = list(current_pkg.get("zone") or [])
    if not isinstance(zones, list):
        zones = []

    # ── build new zone list ────────────────────────────────────────────────────
    if params.action == "create":
        zone_id = params.zone_id or _derive_id(params.name or "zone")
        if any(z.get("id") == zone_id for z in zones):
            return ToolResult.error(
                "already_exists",
                f"zone id {zone_id!r} already present — use 'update'",
            )
        new_entry: dict[str, Any] = {
            "id": zone_id,
            "name": params.name,
            "latitude": params.latitude,
            "longitude": params.longitude,
            "radius": params.radius if params.radius is not None else 100.0,
        }
        if params.icon is not None:
            new_entry["icon"] = params.icon
        if params.passive is not None:
            new_entry["passive"] = params.passive
        zones_after = [*zones, new_entry]

    elif params.action == "update":
        assert params.zone_id is not None  # guarded above
        zone_id = params.zone_id
        # Build the replacement entry, preserving existing values for unset fields.
        existing = next((z for z in zones if z.get("id") == zone_id), None)
        if existing is None:
            return ToolResult.error(
                "not_found",
                f"zone id {zone_id!r} not found in {AGENT_PACKAGE_PATH}",
            )
        updated_entry: dict[str, Any] = dict(existing)
        if params.name is not None:
            updated_entry["name"] = params.name
        if params.latitude is not None:
            updated_entry["latitude"] = params.latitude
        if params.longitude is not None:
            updated_entry["longitude"] = params.longitude
        if params.radius is not None:
            updated_entry["radius"] = params.radius
        if params.icon is not None:
            updated_entry["icon"] = params.icon
        if params.passive is not None:
            updated_entry["passive"] = params.passive
        zones_after, replaced = _replace_by_id(zones, zone_id, updated_entry)
        if not replaced:
            return ToolResult.error(
                "not_found",
                f"zone id {zone_id!r} not found in {AGENT_PACKAGE_PATH}",
            )

    else:  # delete
        assert params.zone_id is not None
        zone_id = params.zone_id
        zones_after, removed = _remove_by_id(zones, zone_id)
        if not removed:
            return ToolResult.error(
                "not_found",
                f"zone id {zone_id!r} not found in {AGENT_PACKAGE_PATH}",
            )

    new_pkg = {**current_pkg, "zone": zones_after}
    diff = diff_structs(current_pkg, new_pkg)
    preview_data: dict[str, Any] = {
        "action": params.action,
        "zone_id": zone_id,
        "path": AGENT_PACKAGE_PATH,
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview_data["preview"] = True
        return ToolResult.ok(preview_data)

    new_text = dump_yaml(new_pkg)

    rollback_result = await apply_optimistic_reload_all(
        client=ctx.ws_client,
        path=pkg_path,
        content=new_text,
        config_dir=ctx.config.ha_config_dir,
        mylo_data_dir=ctx.config.mylo_data_dir,
        verify=None,
        reload_wait_seconds=15.0,
        audit=ctx.audit,
        conversation_id=ctx.conversation_id,
        tool_name="modify_zones",
    )

    if not rollback_result.ok:
        return ToolResult.error(
            "apply_failed",
            "write or reload failed — rolled back",
            data={
                **preview_data,
                "preview": False,
                "rollback": rollback_result.to_dict(),
            },
        )

    preview_data["preview"] = False
    return ToolResult.ok(preview_data)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _load_package(path: Path) -> dict[str, Any]:
    if not exists(path):
        return {}
    raw = read_text(path)
    parsed = load_yaml(raw)
    return parsed if isinstance(parsed, dict) else {}


def _derive_id(name: str) -> str:
    return slugify(name, separator="_") or "unnamed_zone"


def _replace_by_id(
    items: list[dict[str, Any]], target_id: str, new_entry: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    replaced = False
    out: list[dict[str, Any]] = []
    for item in items:
        if item.get("id") == target_id:
            out.append(new_entry)
            replaced = True
        else:
            out.append(item)
    return out, replaced


def _remove_by_id(items: list[dict[str, Any]], target_id: str) -> tuple[list[dict[str, Any]], bool]:
    removed = False
    out: list[dict[str, Any]] = []
    for item in items:
        if item.get("id") == target_id and not removed:
            removed = True
            continue
        out.append(item)
    return out, removed


TOOL = ToolDefinition(
    name="modify_zones",
    description=(
        "Create, update, or delete a Mylo-managed HA zone. "
        "Zones define geographic areas used for presence detection and "
        "automations (home, work, school, etc.). They are stored in "
        "/config/packages/agent.yaml under a 'zone:' list. "
        "The built-in 'home' zone cannot be edited — it is managed by HA core. "
        "ALWAYS call with dry_run=true first; present the diff preview to the "
        "user before calling dry_run=false."
    ),
    params_model=ModifyZonesParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
