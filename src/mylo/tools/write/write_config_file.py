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

"""``write_config_file`` — create or replace a YAML file under ``/config/``.

The workhorse tier-2 tool. Every other write tool ultimately ends up
here or in :mod:`patch_config_file`. Flow:

1. Validate the path against the write policy.
2. Parse the proposed content — reject malformed YAML before touching
   disk.
3. If the content declares automations, run them through the
   automation schema validator and the entity-ref resolver.
4. On ``dry_run=True``: build a structured diff against the existing
   file (if any) and return it as a preview. Nothing hits disk.
5. On ``dry_run=False``: write through :func:`apply_with_rollback` —
   atomic write, backup, reload, verify, rollback on failure.

The ``domain`` param tells the rollback loop which HA service to call
for the reload. Defaults to "all" (``homeassistant.reload_all``) which
is always safe but slower; callers who know better can set it to
"automation" etc. to target the reload.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.diff import diff_yaml
from mylo.files.manager import exists, read_text
from mylo.files.rollback import RELOAD_SERVICES, apply_with_rollback
from mylo.resolver.resolver import Resolver
from mylo.safety.file_access import FileAccessError, resolve_under_config_writable
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register
from mylo.validators.automation_schema import validate_automation
from mylo.validators.entity_refs import check_entity_refs
from mylo.validators.yaml_parser import dump_yaml, load_yaml


class WriteConfigFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(
        description=(
            "Path relative to /config/ (e.g. 'packages/agent.yaml'). "
            "configuration.yaml, secrets.yaml, and .storage/ are blocked."
        ),
    )
    content: str = Field(
        description="Full YAML file content. Will be parsed and validated before writing.",
    )
    domain: str = Field(
        default="all",
        description=(
            "HA domain to reload after writing — 'automation', 'script', "
            "'scene', 'template', 'input_boolean', 'lovelace', 'all', etc. "
            "Default 'all' always works but can be slow on large homes."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "If true, validate and return a diff without writing. The model "
            "MUST call with dry_run=true first, present the preview to the "
            "user, and only retry with dry_run=false after explicit approval."
        ),
    )


async def handler(params: WriteConfigFileParams, ctx: ToolContext) -> ToolResult:
    # Resolve + path policy.
    try:
        resolved = resolve_under_config_writable(ctx.config.ha_config_dir, params.path)
    except FileAccessError as exc:
        return ToolResult.error(exc.code, exc.message)

    # Parse proposed content.
    try:
        parsed = load_yaml(params.content)
    except Exception as exc:
        return ToolResult.error("invalid_yaml", f"{type(exc).__name__}: {exc}")

    # Structural validation — only when the file looks like an automation
    # file. This is intentionally narrow; the catch-all write tool should
    # not try to validate every possible HA schema.
    schema_issues: list[dict[str, Any]] = []
    ref_check: dict[str, Any] | None = None
    if _looks_like_automations(parsed):
        report = validate_automation(_automations_list(parsed))
        schema_issues = [i.to_dict() for i in report.issues]
        if not report.ok:
            return ToolResult.error(
                "schema_invalid",
                "one or more schema errors — fix and retry",
                data={"schema_issues": schema_issues},
            )
        ref_result = check_entity_refs(_automations_list(parsed), Resolver(ctx.registries))
        ref_check = ref_result.to_dict()
        if not ref_result.ok:
            return ToolResult.error(
                "references_invalid",
                "unknown entities or broken templates — fix and retry",
                data={"ref_check": ref_check, "schema_issues": schema_issues},
            )

    # Validate domain → HA service mapping.
    if params.domain not in RELOAD_SERVICES:
        return ToolResult.error(
            "unknown_domain",
            f"domain {params.domain!r} not in {sorted(RELOAD_SERVICES)}",
        )

    # Diff for preview.
    old_text = read_text(resolved) if exists(resolved) else None
    # Normalize content through ruamel so the diff doesn't show
    # whitespace-only changes.
    normalized = dump_yaml(parsed) if parsed is not None else params.content
    diff = diff_yaml(old_text, normalized)

    preview: dict[str, Any] = {
        "path": params.path,
        "mode": "create" if old_text is None else "update",
        "domain": params.domain,
        "diff": diff.to_dict(),
        "schema_issues": schema_issues,
    }
    if ref_check is not None:
        preview["ref_check"] = ref_check

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    # Apply. The generic write tool has no domain-specific verifier —
    # modify_automation adds one. A successful reload with no new errors
    # is taken as good enough here.
    rollback_result = await apply_with_rollback(
        client=ctx.ws_client,
        path=resolved,
        content=normalized,
        domain=params.domain,
        config_dir=ctx.config.ha_config_dir,
        mylo_data_dir=ctx.config.mylo_data_dir,
        verify=None,
    )

    envelope: dict[str, Any] = {
        **preview,
        "preview": False,
        "apply": rollback_result.to_dict(),
    }
    if not rollback_result.ok:
        return ToolResult.error(
            "apply_failed",
            "write applied but reload/verify failed — see apply.steps",
            data=envelope,
        )
    return ToolResult.ok(envelope)


# ─── helpers ─────────────────────────────────────────────────────────────────


def _looks_like_automations(parsed: Any) -> bool:
    """Heuristic: the content is an automation list (top-level list) or a
    package with an ``automation:`` key.
    """
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return "trigger" in parsed[0] or "triggers" in parsed[0]
    return isinstance(parsed, dict) and "automation" in parsed


def _automations_list(parsed: Any) -> Any:
    """Return the automation list regardless of list-vs-package shape."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return parsed.get("automation", [])
    return []


TOOL = ToolDefinition(
    name="write_config_file",
    description=(
        "Create or replace a YAML configuration file under /config/. "
        "Validates YAML, automation schemas where applicable, and all "
        "entity references before touching disk. ALWAYS call first with "
        "dry_run=true to show the user a preview; only retry with "
        "dry_run=false after explicit user approval."
    ),
    params_model=WriteConfigFileParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
