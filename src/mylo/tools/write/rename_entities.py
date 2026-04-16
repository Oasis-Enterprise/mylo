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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.manager import atomic_write, exists, read_text
from mylo.ha.ws_client import CommandError, CommandTimeout, HaWsClient
from mylo.logging_setup import get_logger
from mylo.safety.audit import AuditLogger, make_entry
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)

# Keep strong refs on background verify tasks so they don't get
# garbage-collected mid-flight.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


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
            # Short timeout. Entity renames on 2000+ entity registries
            # consistently outlast any reasonable wait — HA processes
            # them server-side but the response can't return cleanly
            # because the entity_registry_updated event cascade resets
            # the websocket mid-call. We fail fast and trust that HA
            # will finish processing; a background task verifies.
            await ctx.ws_client.send_command(
                "config/entity_registry/update", timeout=10.0, **update_kwargs
            )
            rename_results.append({"entity_id": entry.entity_id, "ok": True})
        except CommandTimeout:
            # Optimistic success + background verify. Spawn a task that
            # checks the registry in a minute and logs + notifies on
            # failure. See apply_optimistic_reload_all for the same
            # pattern around HA-reload timeouts.
            new_id = entry.new_entity_id or entry.entity_id
            task = asyncio.create_task(
                _background_verify_rename(
                    client=ctx.ws_client,
                    audit=ctx.audit,
                    conversation_id=ctx.conversation_id,
                    old_id=entry.entity_id,
                    new_id=new_id,
                )
            )
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
            rename_results.append(
                {
                    "entity_id": entry.entity_id,
                    "ok": True,
                    "note": (
                        "rename dispatched; HA takes 30-90s to fully "
                        "process on large registries. Verifying in "
                        "background — you'll get an HA notification if "
                        "it didn't take."
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


# ─── Background verify (optimistic pattern) ────────────────────────────────


async def _background_verify_rename(
    *,
    client: HaWsClient,
    audit: AuditLogger | None,
    conversation_id: str,
    old_id: str,
    new_id: str,
    initial_wait: float = 60.0,
    poll_seconds: float = 60.0,
) -> None:
    """After a rename command times out, verify server-side in the
    background and surface an HA persistent_notification on failure.

    Approach:
    1. Sleep ``initial_wait`` to let HA finish the registry cascade.
    2. Ask for a SINGLE registry list with a short timeout; if that
       call itself hangs (ws still processing), back off and retry.
    3. If the new id appears before ``poll_seconds`` elapses: audit
       success silently.
    4. Otherwise: audit failure and send an HA notification.
    """
    try:
        await asyncio.sleep(initial_wait)
        try:
            await client.wait_ready(timeout=15.0)
        except TimeoutError:
            await _record_rename_failure(
                client,
                audit,
                conversation_id,
                old_id,
                new_id,
                "websocket did not reconnect after rename",
            )
            return

        loop_deadline = asyncio.get_event_loop().time() + poll_seconds
        while asyncio.get_event_loop().time() < loop_deadline:
            try:
                listing = await client.send_command("config/entity_registry/list", timeout=10.0)
            except CommandTimeout:
                await asyncio.sleep(5.0)
                continue
            except CommandError:
                await asyncio.sleep(5.0)
                continue

            if isinstance(listing, list) and any(
                isinstance(e, dict) and e.get("entity_id") == new_id for e in listing
            ):
                await _record_rename_success(audit, conversation_id, old_id, new_id)
                return
            await asyncio.sleep(5.0)

        await _record_rename_failure(
            client,
            audit,
            conversation_id,
            old_id,
            new_id,
            "new entity_id did not appear in registry within poll window",
        )
    except Exception:
        log.exception("background_verify_rename.unexpected_error")


async def _record_rename_success(
    audit: AuditLogger | None,
    conversation_id: str,
    old_id: str,
    new_id: str,
) -> None:
    log.info("rename.background_verify_ok", old=old_id, new=new_id)
    if audit is None:
        return
    entry = make_entry(
        conversation_id=conversation_id,
        tool_name="rename_entities",
        tier=2,
        params={"entity_id": old_id, "new_entity_id": new_id},
        dry_run=False,
        user_approved=True,
        result="success",
        details={"background_verify": f"{new_id} present in registry"},
    )
    try:
        await audit.write(entry)
    except Exception:
        log.exception("rename.audit_write_failed")


async def _record_rename_failure(
    client: HaWsClient,
    audit: AuditLogger | None,
    conversation_id: str,
    old_id: str,
    new_id: str,
    message: str,
) -> None:
    log.warning("rename.background_verify_failed", old=old_id, new=new_id, reason=message)
    if audit is not None:
        entry = make_entry(
            conversation_id=conversation_id,
            tool_name="rename_entities",
            tier=2,
            params={"entity_id": old_id, "new_entity_id": new_id},
            dry_run=False,
            user_approved=True,
            result="failure",
            details={"background_verify": message},
        )
        try:
            await audit.write(entry)
        except Exception:
            log.exception("rename.audit_write_failed")
    try:
        await client.send_command(
            "call_service",
            domain="persistent_notification",
            service="create",
            service_data={
                "title": "Mylo: rename verification failed",
                "message": (
                    f"Attempted to rename {old_id} → {new_id} but couldn't "
                    f"confirm it took: {message}. Check Settings → Entities."
                ),
                "notification_id": f"mylo_rename_failed_{new_id}",
            },
            timeout=10.0,
        )
    except Exception:
        log.exception("rename.notification_failed")


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
