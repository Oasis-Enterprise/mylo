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

"""``modify_script`` — create, update, and delete HA scripts.

Scripts are reusable action sequences without triggers — callable
from automations, dashboards, or via ``call_service``.

Agent-managed scripts live alongside automations in
``/config/packages/agent.yaml`` under a ``script:`` key. Follows
the same hot/cold path split as ``modify_automation``: first write
to a new package triggers ``homeassistant.reload_all``; subsequent
edits use ``script.reload`` for a fast targeted reload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from slugify import slugify

from mylo.files.diff import diff_structs
from mylo.files.manager import exists, read_text
from mylo.files.rollback import apply_optimistic_reload_all
from mylo.logging_setup import get_logger
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register
from mylo.validators.yaml_parser import dump_yaml, load_yaml

log = get_logger(__name__)

Action = Literal["create", "update", "delete"]

AGENT_PACKAGE_PATH = "packages/agent.yaml"


class ModifyScriptParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(description="create | update | delete")
    script_id: str | None = Field(
        default=None,
        description=(
            "Script identifier (the key under `script:`). Required for "
            "update/delete. For create, derived from alias if omitted."
        ),
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The script definition (alias, sequence, mode, etc.) for "
            "create/update. Must include at minimum 'sequence' with a "
            "list of actions. Ignored for delete."
        ),
    )
    dry_run: bool = Field(default=True)


async def handler(params: ModifyScriptParams, ctx: ToolContext) -> ToolResult:
    if params.action in ("create", "update") and not params.config:
        return ToolResult.error(
            "missing_config",
            f"action {params.action!r} requires a 'config' dict with at least 'sequence'",
        )

    pkg_path = ctx.config.ha_config_dir / AGENT_PACKAGE_PATH
    current_pkg = _load_package(pkg_path)
    scripts: dict[str, Any] = current_pkg.get("script") or {}
    if not isinstance(scripts, dict):
        scripts = {}

    if params.action == "create":
        config = params.config or {}
        alias = config.get("alias", "")
        script_id = params.script_id or slugify(alias, separator="_")
        if not script_id:
            return ToolResult.error(
                "missing_param",
                "provide script_id or an alias to derive one from",
            )
        if script_id in scripts:
            return ToolResult.error(
                "already_exists",
                f"script {script_id!r} already exists in agent.yaml",
            )
        new_scripts = dict(scripts)
        new_scripts[script_id] = config

    elif params.action == "update":
        if not params.script_id:
            return ToolResult.error("missing_param", "update requires 'script_id'")
        if params.script_id not in scripts:
            return ToolResult.error(
                "not_found",
                f"script {params.script_id!r} not found in agent.yaml",
                data={"available": sorted(scripts.keys())},
            )
        script_id = params.script_id
        new_scripts = dict(scripts)
        new_scripts[script_id] = params.config

    elif params.action == "delete":
        if not params.script_id:
            return ToolResult.error("missing_param", "delete requires 'script_id'")
        if params.script_id not in scripts:
            return ToolResult.error(
                "not_found",
                f"script {params.script_id!r} not found in agent.yaml",
                data={"available": sorted(scripts.keys())},
            )
        script_id = params.script_id
        new_scripts = {k: v for k, v in scripts.items() if k != script_id}

    else:
        return ToolResult.error("invalid_action", f"unknown action {params.action!r}")

    new_pkg = dict(current_pkg)
    new_pkg["script"] = new_scripts

    diff = diff_structs(current_pkg, new_pkg)
    preview: dict[str, Any] = {
        "action": params.action,
        "script_id": script_id,
        "path": AGENT_PACKAGE_PATH,
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    new_text = dump_yaml(new_pkg)

    # Hot/cold path — same split as modify_automation.
    # Both hot and cold paths use the optimistic pattern: write, fire
    # reload, return immediately, verify in background. On large homes
    # even targeted reloads can take 30-60s.
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
        tool_name="modify_script",
    )

    if not rollback_result.ok:
        return ToolResult.error(
            "apply_failed",
            "write or reload failed — rolled back",
            data={
                **preview,
                "preview": False,
                "rollback": rollback_result.to_dict(),
            },
        )

    preview["preview"] = False
    return ToolResult.ok(preview)


def _load_package(path: Path) -> dict[str, Any]:
    if not exists(path):
        return {}
    raw = read_text(path)
    parsed = load_yaml(raw)
    return parsed if isinstance(parsed, dict) else {}


TOOL = ToolDefinition(
    name="modify_script",
    description=(
        "Create, update, or delete Home Assistant scripts. Scripts are "
        "reusable action sequences (no triggers) — callable from "
        "automations, dashboards, or via call_service. ALWAYS dry_run=true "
        "first. Scripts are stored in packages/agent.yaml alongside "
        "automations."
    ),
    params_model=ModifyScriptParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
