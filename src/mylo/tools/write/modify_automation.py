"""``modify_automation`` — create / update / delete / enable / disable an
automation.

M7a scope is YAML-mode only. All agent-managed automations live in
``/config/packages/agent.yaml`` as a single package file:

    automation:
      - id: mylo_kitchen_lights_11pm
        alias: Kitchen Lights Off at 11pm
        trigger: ...
        action: ...

The user needs ``packages`` loaded in their configuration.yaml for this
file to be picked up. Typically one line::

    homeassistant:
      packages: !include_dir_named packages

Storage-mode automations (those edited through the HA UI) aren't
touched by this tool — use the read tools to inspect them, or use
``write_config_file`` / ``patch_config_file`` with a user-chosen path
to edit YAML-defined automations that live elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from slugify import slugify

from mylo.files.diff import diff_structs
from mylo.files.manager import exists, read_text
from mylo.files.rollback import apply_with_rollback, automation_loaded_verifier
from mylo.resolver.resolver import Resolver
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register
from mylo.validators.automation_schema import validate_automation
from mylo.validators.entity_refs import check_entity_refs
from mylo.validators.yaml_parser import dump_yaml, load_yaml

Action = Literal["create", "update", "delete", "enable", "disable"]

# Single place Mylo's agent-managed automations live. Relative to
# /config/.
AGENT_PACKAGE_PATH = "packages/agent.yaml"

HEADER_COMMENT = """\
# Managed by Mylo.
# Safe to edit manually — Mylo will detect your changes on next sync.
# To stop Mylo from editing this file, rename it or move it out of
# /config/packages/.
"""


class ModifyAutomationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(description="create | update | delete | enable | disable")
    automation_id: str | None = Field(
        default=None,
        description=(
            "Identifier matching the automation's `id:` field. Required for "
            "update/delete/enable/disable; optional for create (derived from "
            "alias if omitted)."
        ),
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The automation dict (with trigger/condition/action) for create "
            "and update. Ignored for delete/enable/disable."
        ),
    )
    dry_run: bool = Field(default=True)


async def handler(params: ModifyAutomationParams, ctx: ToolContext) -> ToolResult:
    if params.action in ("create", "update") and not params.config:
        return ToolResult.error(
            "missing_config",
            f"action {params.action!r} requires a 'config' dict",
        )
    if params.action in ("update", "delete", "enable", "disable") and not params.automation_id:
        return ToolResult.error(
            "missing_id",
            f"action {params.action!r} requires 'automation_id'",
        )

    pkg_path = ctx.config.ha_config_dir / AGENT_PACKAGE_PATH
    package = _load_package(pkg_path)
    automations_before = list(package.get("automation", []))

    if params.action == "create":
        automation_id = params.automation_id or _derive_id(params.config or {})
        if any(a.get("id") == automation_id for a in automations_before):
            return ToolResult.error(
                "already_exists",
                f"automation id {automation_id!r} already present — use 'update'",
            )
        assert params.config is not None
        new_entry = {**params.config, "id": automation_id}
        automations_after = [*automations_before, new_entry]
    elif params.action == "update":
        assert params.config is not None
        assert params.automation_id is not None  # guarded above
        new_entry = {**params.config, "id": params.automation_id}
        automations_after, replaced = _replace_by_id(
            automations_before, params.automation_id, new_entry
        )
        if not replaced:
            return ToolResult.error(
                "not_found",
                f"automation id {params.automation_id!r} not found in {AGENT_PACKAGE_PATH}",
            )
        automation_id = params.automation_id
    elif params.action == "delete":
        assert params.automation_id is not None
        automations_after, removed = _remove_by_id(automations_before, params.automation_id)
        if not removed:
            return ToolResult.error(
                "not_found",
                f"automation id {params.automation_id!r} not found",
            )
        automation_id = params.automation_id
    else:  # enable / disable
        assert params.automation_id is not None
        mode = params.action == "enable"
        automations_after, flipped = _flip_enabled(automations_before, params.automation_id, mode)
        if not flipped:
            return ToolResult.error(
                "not_found",
                f"automation id {params.automation_id!r} not found",
            )
        automation_id = params.automation_id

    # Validate + ref check the full target list (catches create/update issues).
    schema_issues: list[dict[str, Any]] = []
    ref_check: dict[str, Any] | None = None
    if params.action in ("create", "update"):
        assert params.config is not None
        report = validate_automation(params.config)
        schema_issues = [i.to_dict() for i in report.issues]
        if not report.ok:
            return ToolResult.error(
                "schema_invalid",
                "automation schema has errors — fix and retry",
                data={"schema_issues": schema_issues},
            )
        ref_result = check_entity_refs(params.config, Resolver(ctx.registries))
        ref_check = ref_result.to_dict()
        if not ref_result.ok:
            return ToolResult.error(
                "references_invalid",
                "unknown entities or broken templates — fix and retry",
                data={"schema_issues": schema_issues, "ref_check": ref_check},
            )

    # Build the outgoing package document.
    new_package = {**package, "automation": automations_after}
    new_text = HEADER_COMMENT + dump_yaml(new_package)

    diff = diff_structs(package, new_package)
    preview: dict[str, Any] = {
        "action": params.action,
        "automation_id": automation_id,
        "path": AGENT_PACKAGE_PATH,
        "diff": diff.to_dict(),
        "schema_issues": schema_issues,
    }
    if ref_check is not None:
        preview["ref_check"] = ref_check

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    # Apply. Verifier checks the automation entity exists and is in a
    # healthy state after reload. For delete we skip the verifier — the
    # entity shouldn't exist afterward, and a generic "reload returned"
    # signal is enough.
    verify = (
        automation_loaded_verifier(f"automation.{_slug(automation_id)}")
        if params.action in ("create", "update", "enable")
        else None
    )
    # Use reload_all (not reload_automation) because we write to a
    # packages/*.yaml file. HA's automation reload only re-reads the
    # `automation:` source; it does NOT re-process the `packages:`
    # directive. For a newly-created package file to be picked up, the
    # package merge stage has to run again — that's what reload_all does.
    # Slower (~5-10s on busy homes) but correctness beats speed here.
    rollback_result = await apply_with_rollback(
        client=ctx.ws_client,
        path=pkg_path,
        content=new_text,
        domain="all",
        config_dir=ctx.config.ha_config_dir,
        mylo_data_dir=ctx.config.mylo_data_dir,
        verify=verify,
        reload_wait_seconds=15.0,
    )
    envelope: dict[str, Any] = {
        **preview,
        "preview": False,
        "apply": rollback_result.to_dict(),
    }
    if not rollback_result.ok:
        return ToolResult.error(
            "apply_failed",
            "apply pipeline failed — see apply.steps for diagnosis",
            data=envelope,
        )
    return ToolResult.ok(envelope)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _load_package(pkg_path: Path) -> dict[str, Any]:
    if not exists(pkg_path):
        return {}
    raw = read_text(pkg_path)
    parsed = load_yaml(raw) or {}
    if not isinstance(parsed, dict):
        return {}
    if "automation" not in parsed or not isinstance(parsed["automation"], list):
        parsed["automation"] = []
    return parsed


def _derive_id(config: dict[str, Any]) -> str:
    alias = str(config.get("alias") or "automation").strip()
    return f"mylo_{_slug(alias)}"


def _slug(value: str | None) -> str:
    return slugify(str(value or ""), separator="_") or "unnamed"


def _replace_by_id(
    items: list[dict[str, Any]], target_id: str | None, new_entry: dict[str, Any]
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


def _remove_by_id(
    items: list[dict[str, Any]], target_id: str | None
) -> tuple[list[dict[str, Any]], bool]:
    removed = False
    out: list[dict[str, Any]] = []
    for item in items:
        if item.get("id") == target_id and not removed:
            removed = True
            continue
        out.append(item)
    return out, removed


def _flip_enabled(
    items: list[dict[str, Any]], target_id: str | None, enabled: bool
) -> tuple[list[dict[str, Any]], bool]:
    flipped = False
    out: list[dict[str, Any]] = []
    for item in items:
        if item.get("id") == target_id:
            new_item = dict(item)
            new_item["initial_state"] = enabled
            out.append(new_item)
            flipped = True
        else:
            out.append(item)
    return out, flipped


TOOL = ToolDefinition(
    name="modify_automation",
    description=(
        "Create, update, delete, enable, or disable a Mylo-managed "
        "automation. M7a scope: YAML mode only — automations live in "
        "/config/packages/agent.yaml. Requires the user to have 'packages' "
        "loaded in configuration.yaml. ALWAYS call dry_run=true first, "
        "present the preview to the user, and only retry with dry_run=false "
        "after explicit user approval."
    ),
    params_model=ModifyAutomationParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
