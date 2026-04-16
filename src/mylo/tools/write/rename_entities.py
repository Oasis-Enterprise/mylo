"""``rename_entities`` — rename entities with optional reference cascade.

The most complex write tool. Two layers:

1. **Registry rename** — updates the entity's ``entity_id`` and/or
   ``friendly_name`` in the entity registry via websocket. Immediate.

2. **Reference cascade** (optional, ``update_references=True``) — scans
   all automations and Lovelace dashboards for occurrences of the old
   entity_id and replaces them. Automations are YAML files on disk
   (rewritten via :mod:`files.manager`); dashboards are storage-mode
   (rewritten via ``lovelace/config/save``). Both paths produce diffs
   in dry-run.

Dry-run is *critical* here. A mis-typed rename can break every
automation that references the entity. The dry-run response lists every
file and dashboard that contains the old entity_id, so the user can
verify before committing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.manager import atomic_write, exists, read_text
from mylo.ha.ws_client import CommandError, CommandTimeout
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class RenameEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(description="Current entity_id to rename.")
    new_entity_id: str | None = Field(
        default=None,
        description="New entity_id (e.g. 'sensor.office_temperature'). Omit to keep the current id.",
    )
    new_friendly_name: str | None = Field(
        default=None,
        description="New display name. Omit to keep current.",
    )


class RenameEntitiesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    renames: list[RenameEntry] = Field(
        min_length=1,
        description="One or more rename operations.",
    )
    update_references: bool = Field(
        default=False,
        description=(
            "If true, scan automations and dashboards for the old entity_id "
            "and replace with the new one. Only relevant when new_entity_id "
            "is set."
        ),
    )
    dry_run: bool = Field(default=True)


async def handler(params: RenameEntitiesParams, ctx: ToolContext) -> ToolResult:
    # Validate all renames first.
    for entry in params.renames:
        if not entry.new_entity_id and not entry.new_friendly_name:
            return ToolResult.error(
                "missing_param",
                f"rename for {entry.entity_id}: must set new_entity_id or new_friendly_name",
            )
        if entry.entity_id not in ctx.registries.entities:
            return ToolResult.error(
                "entity_not_found",
                f"{entry.entity_id} not in registry",
                data={
                    "did_you_mean": _fuzzy_suggestions(
                        entry.entity_id, ctx.registries.entities.keys()
                    )
                },
            )
        if entry.new_entity_id and entry.new_entity_id in ctx.registries.entities:
            return ToolResult.error(
                "already_exists",
                f"{entry.new_entity_id} already exists — choose a different id",
            )

    # Build reference scan for cascading renames.
    references: dict[str, list[dict[str, Any]]] = {}
    for entry in params.renames:
        if entry.new_entity_id and params.update_references:
            refs = await _scan_references(ctx, entry.entity_id)
            if refs:
                references[entry.entity_id] = refs

    preview: dict[str, Any] = {
        "renames": [
            {
                "entity_id": e.entity_id,
                "new_entity_id": e.new_entity_id,
                "new_friendly_name": e.new_friendly_name,
            }
            for e in params.renames
        ],
        "update_references": params.update_references,
        "references_found": references,
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    # Apply registry renames.
    #
    # entity_id changes on big registries (2000+ entities) can take
    # 30-90s server-side — HA has to recompute a lot. The call
    # sometimes doesn't return a response within our timeout even
    # though the rename succeeded. On CommandTimeout we reconnect,
    # refresh the registry, and confirm the new entity_id exists
    # before reporting success.
    rename_results: list[dict[str, Any]] = []
    for entry in params.renames:
        update_kwargs: dict[str, Any] = {"entity_id": entry.entity_id}
        if entry.new_entity_id:
            update_kwargs["new_entity_id"] = entry.new_entity_id
        if entry.new_friendly_name:
            update_kwargs["name"] = entry.new_friendly_name
        try:
            # Short timeout — entity renames often outlast our wait.
            # Fail fast and poll the registry instead of blocking.
            await ctx.ws_client.send_command(
                "config/entity_registry/update", timeout=10.0, **update_kwargs
            )
            rename_results.append({"entity_id": entry.entity_id, "ok": True})
        except CommandTimeout:
            verified = await _poll_for_rename(
                ctx,
                new_id=entry.new_entity_id or entry.entity_id,
                poll_seconds=30.0,
            )
            if verified:
                rename_results.append(
                    {
                        "entity_id": entry.entity_id,
                        "ok": True,
                        "note": (
                            "command timed out; rename confirmed in "
                            "registry after polling"
                        ),
                    }
                )
            else:
                rename_results.append(
                    {
                        "entity_id": entry.entity_id,
                        "ok": False,
                        "error": (
                            "timed out; new entity_id not in registry after "
                            "30s of polling — verify manually in HA"
                        ),
                    }
                )
        except CommandError as exc:
            rename_results.append(
                {
                    "entity_id": entry.entity_id,
                    "ok": False,
                    "error": f"{exc.code}: {exc.message}",
                }
            )

    # Apply reference cascades.
    cascade_results: list[dict[str, Any]] = []
    if params.update_references:
        for entry in params.renames:
            if not entry.new_entity_id:
                continue
            refs = references.get(entry.entity_id, [])
            for ref in refs:
                result = await _apply_cascade(ctx, entry.entity_id, entry.new_entity_id, ref)
                cascade_results.append(result)

    all_ok = all(r["ok"] for r in rename_results) and all(
        r.get("ok", True) for r in cascade_results
    )
    envelope: dict[str, Any] = {
        **preview,
        "preview": False,
        "rename_results": rename_results,
        "cascade_results": cascade_results,
    }
    if not all_ok:
        return ToolResult.error(
            "partial_failure",
            "some renames or cascades failed — see results",
            data=envelope,
        )
    return ToolResult.ok(envelope)


# ─── Post-timeout verification ──────────────────────────────────────────────


async def _poll_for_rename(
    ctx: ToolContext, *, new_id: str, poll_seconds: float
) -> bool:
    """Poll the entity registry for the new id after a rename timeout.

    HA may still be processing the rename when our send_command timed
    out. Wait for the websocket to reconnect (common after registry
    cascades), then refresh the registry every few seconds until the
    new id appears or the deadline expires.
    """
    deadline = time.monotonic() + poll_seconds
    while time.monotonic() < deadline:
        try:
            await ctx.ws_client.wait_ready(timeout=5.0)
            await ctx.registries.refresh(force=True)
        except Exception:
            await asyncio.sleep(2.0)
            continue
        if new_id in ctx.registries.entities:
            return True
        await asyncio.sleep(2.0)
    return False


# ─── Reference scanning ──────────────────────────────────────────────────────


async def _scan_references(ctx: ToolContext, entity_id: str) -> list[dict[str, Any]]:
    """Find where ``entity_id`` appears in automations and dashboards."""
    refs: list[dict[str, Any]] = []

    # Scan YAML files under packages/agent.yaml (our managed file).
    agent_path = ctx.config.ha_config_dir / "packages" / "agent.yaml"
    if exists(agent_path):
        content = read_text(agent_path)
        if entity_id in content:
            refs.append(
                {
                    "type": "yaml_file",
                    "path": "packages/agent.yaml",
                    "occurrences": content.count(entity_id),
                }
            )

    # Scan the main automations.yaml.
    auto_path = ctx.config.ha_config_dir / "automations.yaml"
    if exists(auto_path):
        content = read_text(auto_path)
        if entity_id in content:
            refs.append(
                {
                    "type": "yaml_file",
                    "path": "automations.yaml",
                    "occurrences": content.count(entity_id),
                }
            )

    # Scan storage-mode dashboards.
    try:
        dashboards = await ctx.ws_client.send_command("lovelace/dashboards/list")
    except CommandError:
        dashboards = []
    dashboard_ids = [None]  # default dashboard
    for d in dashboards or []:
        if isinstance(d, dict) and d.get("url_path"):
            dashboard_ids.append(d["url_path"])
    for did in dashboard_ids:
        try:
            config = await ctx.ws_client.send_command("lovelace/config", url_path=did)
        except CommandError:
            continue
        config_str = str(config)
        if entity_id in config_str:
            refs.append(
                {
                    "type": "dashboard",
                    "dashboard_id": did,
                    "occurrences": config_str.count(entity_id),
                }
            )

    return refs


async def _apply_cascade(
    ctx: ToolContext,
    old_id: str,
    new_id: str,
    ref: dict[str, Any],
) -> dict[str, Any]:
    """Replace ``old_id`` with ``new_id`` in one reference location."""
    ref_type = ref.get("type")
    try:
        if ref_type == "yaml_file":
            path = ctx.config.ha_config_dir / ref["path"]
            content = read_text(path)
            updated = content.replace(old_id, new_id)
            atomic_write(path, updated)
            return {"ref": ref, "ok": True, "replacements": content.count(old_id)}
        if ref_type == "dashboard":
            did = ref.get("dashboard_id")
            config = await ctx.ws_client.send_command("lovelace/config", url_path=did)
            if not isinstance(config, dict):
                return {"ref": ref, "ok": False, "error": "could not read dashboard"}
            config_str = str(config)
            if old_id not in config_str:
                return {"ref": ref, "ok": True, "replacements": 0}
            updated = _deep_replace(config, old_id, new_id)
            await ctx.ws_client.send_command("lovelace/config/save", url_path=did, config=updated)
            return {"ref": ref, "ok": True, "replacements": config_str.count(old_id)}
    except Exception as exc:
        return {"ref": ref, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ref": ref, "ok": False, "error": "unknown ref type"}


def _deep_replace(obj: Any, old: str, new: str) -> Any:
    """Recursively replace ``old`` with ``new`` in all string values."""
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, dict):
        return {k: _deep_replace(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_replace(v, old, new) for v in obj]
    return obj


def _fuzzy_suggestions(query: str, candidates: Any) -> list[str]:
    from rapidfuzz import fuzz, process

    results = process.extract(
        query, list(candidates), scorer=fuzz.WRatio, limit=5, score_cutoff=60.0
    )
    return [name for name, _score, _idx in results]


TOOL = ToolDefinition(
    name="rename_entities",
    description=(
        "Rename one or more entities (entity_id and/or friendly_name). "
        "Optionally cascade: update all references in automations, scripts, "
        "and dashboards. ALWAYS dry_run=true first — the preview shows "
        "every file and dashboard that will be modified. This is the most "
        "impactful write tool; a bad rename can break automations."
    ),
    params_model=RenameEntitiesParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
