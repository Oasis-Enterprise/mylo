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

"""``modify_scene`` — create, update, delete, or activate HA scenes.

Scenes snapshot a group of entity states so they can be recalled with a
single command. Agent-managed scenes live alongside automations and scripts
in ``/config/packages/agent.yaml`` under a ``scene:`` list key:

    scene:
      - id: "evening_chill"
        name: "Evening Chill"
        entities:
          light.kitchen:
            state: "on"
            brightness: 200
          switch.fan: "off"

The ``activate`` action does not edit YAML — it fires a ``scene.turn_on``
service call via the websocket (like ``call_service``) and is gated by the
same tier-2 approval requirement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from slugify import slugify

from mylo.files.diff import diff_structs
from mylo.files.manager import exists, read_text
from mylo.files.rollback import apply_optimistic_reload_all
from mylo.ha.states import get_all_states
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register
from mylo.validators.yaml_parser import dump_yaml, load_yaml

Action = Literal["create", "update", "delete", "activate"]

AGENT_PACKAGE_PATH = "packages/agent.yaml"


class ModifySceneParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(description="create | update | delete | activate")
    scene_id: str | None = Field(
        default=None,
        description=(
            "Scene identifier matching the scene's `id:` field. Required for "
            "update/delete/activate. For create, derived from name if omitted."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Human-readable scene name. Required for create and update.",
    )
    entities: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Mapping of entity_id → state. The value may be a plain state "
            "string (e.g. 'off') or a dict with 'state' plus attribute keys "
            "(e.g. {'state': 'on', 'brightness': 200}). Required for create "
            "if capture_entities is not set."
        ),
    )
    capture_entities: list[str] | None = Field(
        default=None,
        description=(
            "On create or update: fetch the listed entities' current "
            "state and attributes from HA and snapshot them as the "
            "scene's entity data. Merged with any explicit 'entities' "
            "values (explicit wins on conflict). Convenient for 'save "
            "what's on right now as a scene'."
        ),
    )
    dry_run: bool = Field(default=True)


async def handler(params: ModifySceneParams, ctx: ToolContext) -> ToolResult:
    # ── activate path: service call, no YAML edit ─────────────────────────
    if params.action == "activate":
        if not params.scene_id:
            return ToolResult.error("missing_param", "activate requires 'scene_id'")

        entity_id = f"scene.{params.scene_id}"
        preview: dict[str, Any] = {
            "action": "activate",
            "scene_id": params.scene_id,
            "entity_id": entity_id,
        }

        if params.dry_run:
            preview["preview"] = True
            return ToolResult.ok(preview)

        await ctx.ws_client.send_command(
            "call_service",
            write=True,
            domain="scene",
            service="turn_on",
            target={"entity_id": entity_id},
        )
        return ToolResult.ok({"activated": entity_id, "preview": False})

    # ── YAML edit path: create / update / delete ───────────────────────────
    if params.action in ("create", "update"):
        if not params.name:
            return ToolResult.error(
                "missing_param",
                f"action {params.action!r} requires 'name'",
            )
        if not params.entities and not params.capture_entities:
            return ToolResult.error(
                "missing_param",
                f"action {params.action!r} requires 'entities' or 'capture_entities'",
            )

    if params.action in ("update", "delete") and not params.scene_id:
        return ToolResult.error(
            "missing_param",
            f"action {params.action!r} requires 'scene_id'",
        )

    # Resolve capture_entities → snapshot current state from HA.
    resolved_entities: dict[str, Any] = {}
    if params.capture_entities and params.action in ("create", "update"):
        all_states = await get_all_states(ctx.ws_client)
        for eid in params.capture_entities:
            state_obj = all_states.get(eid)
            if state_obj is not None:
                entry: dict[str, Any] = {"state": state_obj.get("state", "unknown")}
                attrs = state_obj.get("attributes") or {}
                entry.update(attrs)
                resolved_entities[eid] = entry
            else:
                # Entity not found in live states — include as unknown.
                resolved_entities[eid] = {"state": "unknown"}

    # Explicit entities override captured values on conflict.
    if params.entities:
        resolved_entities.update(params.entities)

    pkg_path = ctx.config.ha_config_dir / AGENT_PACKAGE_PATH
    current_pkg = _load_package(pkg_path)
    scenes: list[dict[str, Any]] = list(current_pkg.get("scene") or [])
    if not isinstance(scenes, list):
        scenes = []

    if params.action == "create":
        scene_id = params.scene_id or _derive_id(params.name or "scene")
        if any(s.get("id") == scene_id for s in scenes):
            return ToolResult.error(
                "already_exists",
                f"scene id {scene_id!r} already present — use 'update'",
            )
        new_entry: dict[str, Any] = {
            "id": scene_id,
            "name": params.name,
            "entities": resolved_entities,
        }
        scenes_after = [*scenes, new_entry]

    elif params.action == "update":
        assert params.scene_id is not None  # guarded above
        scene_id = params.scene_id
        new_entry = {
            "id": scene_id,
            "name": params.name,
            "entities": resolved_entities,
        }
        scenes_after, replaced = _replace_by_id(scenes, scene_id, new_entry)
        if not replaced:
            return ToolResult.error(
                "not_found",
                f"scene id {scene_id!r} not found in {AGENT_PACKAGE_PATH}",
            )

    else:  # delete
        assert params.scene_id is not None
        scene_id = params.scene_id
        scenes_after, removed = _remove_by_id(scenes, scene_id)
        if not removed:
            return ToolResult.error(
                "not_found",
                f"scene id {scene_id!r} not found in {AGENT_PACKAGE_PATH}",
            )

    new_pkg = {**current_pkg, "scene": scenes_after}
    diff = diff_structs(current_pkg, new_pkg)
    preview_data: dict[str, Any] = {
        "action": params.action,
        "scene_id": scene_id,
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
        tool_name="modify_scene",
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
    return slugify(name, separator="_") or "unnamed_scene"


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
    name="modify_scene",
    description=(
        "Create, update, delete, or activate a Mylo-managed HA scene. "
        "Scenes are stored in /config/packages/agent.yaml under a 'scene:' "
        "list. Use 'capture_entities' on create/update to snapshot the "
        "current state of listed entities directly from HA. "
        "'activate' fires scene.turn_on (no YAML change). "
        "ALWAYS call with dry_run=true first for create/update/delete; "
        "present the diff preview to the user before calling dry_run=false."
    ),
    params_model=ModifySceneParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
