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

"""Memory endpoints — status, full view, sync, prune, item delete,
conflict resolution.

``POST /api/memory/sync`` triggers a reconciliation pass. The
remaining endpoints let the Memory tab render the current context
file and apply corrective edits (delete, resolve conflict). Per-item
*edit* is out of scope for M8c — the user can delete and re-capture
via chat, which keeps the mutation surface narrow.
"""

from __future__ import annotations

import json
from typing import cast

from aiohttp import web

from mylo.logging_setup import get_logger
from mylo.memory.pruner import PruneReport, apply_prune, plan_prune
from mylo.memory.reconciler import ReconcileProvider, run_sync
from mylo.memory.schema import MemoryFile
from mylo.memory.scratchpad import drain_scratchpad, read_scratchpad

log = get_logger(__name__)


def register_memory_routes(app: web.Application) -> None:
    app.router.add_get("/api/memory", _handle_get_memory)
    app.router.add_get("/api/memory/full", _handle_get_full)
    app.router.add_get("/api/memory/scratchpad", _handle_get_scratchpad)
    app.router.add_post("/api/memory/sync", _handle_sync)
    app.router.add_post("/api/memory/prune", _handle_prune)
    app.router.add_delete("/api/memory/item", _handle_delete_item)
    app.router.add_post("/api/memory/conflict/{conflict_id}/resolve", _handle_resolve_conflict)
    app.router.add_get("/api/suggestions", _handle_get_suggestions)
    app.router.add_post("/api/suggestions/{suggestion_id}/respond", _handle_suggestion_respond)
    app.router.add_post("/api/suggestions/{suggestion_id}/automated", _handle_suggestion_automated)


# ─── Read endpoints ─────────────────────────────────────────────────────────


async def _handle_get_memory(request: web.Request) -> web.Response:
    from mylo.server.app import AppKeys

    store = request.app[AppKeys.MEMORY]
    memory = store.current()
    return web.json_response(
        {
            "version": memory.version,
            "last_sync": memory.last_sync,
            "counts": {
                "notes": len(memory.notes),
                "known_issues": len(memory.known_issues),
                "patterns": len(memory.patterns),
                "rejected": len(memory.rejected),
                "conflicts": len(memory.conflicts),
                "household_members": len(memory.household.members),
            },
            "pending_conflicts": len(memory.pending_conflicts()),
        }
    )


async def _handle_get_scratchpad(request: web.Request) -> web.Response:
    """Return pending scratchpad entries — notes captured in chat that
    haven't been folded into ``context.yaml`` yet.

    These are already visible to future chat turns (memory injection
    reads scratchpad live), but they don't appear in the Memory tab's
    main sections until the user runs Sync. We surface them as a
    "pending" list so the user sees their notes land instantly.
    """
    from mylo.server.app import AppKeys

    config = request.app[AppKeys.CONFIG]
    entries = read_scratchpad(config.mylo_data_dir)
    return web.json_response(
        {
            "entries": [
                {
                    "type": e.type,
                    "content": e.content,
                    "scope": e.scope,
                    "recorded": e.recorded,
                    "confidence": e.confidence,
                    "conversation_id": e.conversation_id,
                }
                for e in entries
            ]
        }
    )


async def _handle_get_full(request: web.Request) -> web.Response:
    """Return the complete memory document as JSON for the Memory tab.

    ``exclude_none=False`` so the UI sees empty sections explicitly
    (renders "no notes yet" vs. crashing on undefined).
    """
    from mylo.server.app import AppKeys

    store = request.app[AppKeys.MEMORY]
    memory = store.current()
    return web.json_response(memory.model_dump(exclude_none=False, mode="json"))


# ─── Sync ───────────────────────────────────────────────────────────────────


async def _handle_sync(request: web.Request) -> web.Response:
    from mylo.server.app import AppKeys

    try:
        body = await request.json() if request.can_read_body else {}
    except json.JSONDecodeError:
        body = {}

    should_apply_prune = bool(body.get("apply_prune", False))

    store = request.app[AppKeys.MEMORY]
    provider = request.app.get(AppKeys.PROVIDER)
    registries = request.app.get(AppKeys.REGISTRIES)
    config = request.app[AppKeys.CONFIG]

    try:
        result = await run_sync(
            store=store,
            provider=cast("ReconcileProvider | None", provider),
            registries=registries,
            model=config.reconciliation_model,
            mylo_data_dir=config.mylo_data_dir,
        )
    except Exception as exc:
        log.exception("memory.sync_failed")
        return web.json_response(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status=500,
        )

    applied = False
    drained = 0
    if result.updated is not None:
        memory_to_save = result.updated
        if should_apply_prune and result.prune_report.total > 0:
            memory_to_save = apply_prune(memory_to_save, result.prune_report)
            applied = True
        await store.save(memory_to_save, note=f"sync: {result.summary}")
        # Only drain after the save committed — a crash mid-save
        # would otherwise lose the scratchpad without the merge
        # having landed on disk.
        drained = drain_scratchpad(config.mylo_data_dir)
    elif should_apply_prune and result.prune_report.total > 0:
        current = store.current()
        pruned = apply_prune(current, result.prune_report)
        await store.save(
            pruned,
            note=f"prune-only: dropped {result.prune_report.total} items",
        )
        applied = True

    return web.json_response(
        {
            "ok": True,
            "changed": result.changed,
            "applied": applied,
            "summary": result.summary,
            "conflicts_added": result.conflicts_added,
            "prune_candidates": _serialize_prune(result.prune_report),
            "scratchpad_drained": drained,
        }
    )


# ─── Prune-only (manual trigger) ────────────────────────────────────────────


async def _handle_prune(request: web.Request) -> web.Response:
    """Apply the current pruner plan without running the reconciler.

    The Memory tab shows candidates after a sync; clicking "Apply
    prune" ships them here so they get dropped even if nothing else
    changed. An optional ``ids`` list narrows the drop to a chosen
    subset — useful for "reject just this one candidate" flows.
    """
    from mylo.server.app import AppKeys

    try:
        body = await request.json() if request.can_read_body else {}
    except json.JSONDecodeError:
        body = {}

    ids_filter = body.get("ids")  # list[str] | None
    include_low_reference = bool(body.get("include_low_reference", False))
    low_ref_budget = int(body.get("low_reference_budget", 20)) if include_low_reference else None

    store = request.app[AppKeys.MEMORY]
    memory = store.current()

    report = plan_prune(memory, target_budget=low_ref_budget)

    if ids_filter:
        allow = set(ids_filter)
        report.candidates = [c for c in report.candidates if c.item_id in allow]

    if report.total == 0:
        return web.json_response({"ok": True, "applied": 0, "prune_candidates": []})

    pruned = apply_prune(memory, report)
    await store.save(pruned, note=f"manual prune: {report.total} items")

    return web.json_response(
        {
            "ok": True,
            "applied": report.total,
            "prune_candidates": _serialize_prune(report),
        }
    )


# ─── Item delete ────────────────────────────────────────────────────────────


_DELETABLE_SECTIONS = {"notes", "known_issues", "patterns", "rejected"}


async def _handle_delete_item(request: web.Request) -> web.Response:
    """Remove a single item from a section. Body: ``{section, id}``.

    Deleting a user-confirmed note is allowed here — the user is
    explicitly overriding the protection. The pruner still won't touch
    those automatically, but a manual delete is valid consent.
    """
    from mylo.server.app import AppKeys

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    section = body.get("section")
    item_id = body.get("id")
    if section not in _DELETABLE_SECTIONS or not isinstance(item_id, str) or not item_id:
        return web.json_response(
            {"ok": False, "error": "invalid_section_or_id", "allowed": sorted(_DELETABLE_SECTIONS)},
            status=400,
        )

    store = request.app[AppKeys.MEMORY]
    memory = store.current()
    data = memory.model_dump()

    before = len(data.get(section) or [])
    data[section] = [item for item in (data.get(section) or []) if item.get("id") != item_id]
    after = len(data[section])
    if before == after:
        return web.json_response({"ok": False, "error": "not_found"}, status=404)

    updated = MemoryFile.model_validate(data)
    await store.save(updated, note=f"delete {section}/{item_id}")

    return web.json_response({"ok": True, "section": section, "id": item_id})


# ─── Conflict resolution ────────────────────────────────────────────────────


async def _handle_resolve_conflict(request: web.Request) -> web.Response:
    """Resolve a pending conflict.

    Body: ``{"choice": "a" | "b" | "dismiss"}``. "a" keeps claim_a as
    truth and marks the conflict resolved; "b" keeps claim_b; "dismiss"
    marks resolved without picking a side (when neither is worth
    keeping). The reconciler sees resolved conflicts next run and
    won't re-raise the same pairing.
    """
    from mylo.server.app import AppKeys

    conflict_id = request.match_info["conflict_id"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    choice = body.get("choice")
    if choice not in ("a", "b", "dismiss"):
        return web.json_response(
            {"ok": False, "error": "choice must be 'a', 'b', or 'dismiss'"},
            status=400,
        )

    store = request.app[AppKeys.MEMORY]
    memory = store.current()
    data = memory.model_dump()

    target = None
    for conflict in data.get("conflicts") or []:
        if conflict.get("id") == conflict_id:
            target = conflict
            break
    if target is None:
        return web.json_response({"ok": False, "error": "conflict_not_found"}, status=404)

    target["status"] = "resolved"
    target["resolution"] = {
        "choice": choice,
        "kept": target.get(f"claim_{choice}") if choice in ("a", "b") else None,
    }

    updated = MemoryFile.model_validate(data)
    await store.save(updated, note=f"resolve conflict {conflict_id}: {choice}")

    return web.json_response({"ok": True, "id": conflict_id, "choice": choice})


# ─── Suggestions ────────────────────────────────────────────────────────────


async def _handle_get_suggestions(request: web.Request) -> web.Response:
    """Return current suggestion state from memory."""
    from mylo.server.app import AppKeys

    store = request.app[AppKeys.MEMORY]
    memory = store.current()
    return web.json_response(
        {
            "suggestions": [
                {
                    "id": s.id,
                    "type": s.type,
                    "entity_id": s.entity_id,
                    "description": s.description,
                    "times_suggested": s.times_suggested,
                    "times_accepted": s.times_accepted,
                    "times_rejected": s.times_rejected,
                    "automated": s.automated,
                }
                for s in memory.suggestions
            ],
            "count": len(memory.suggestions),
        }
    )


async def _handle_suggestion_respond(request: web.Request) -> web.Response:
    """Record the user's response to a suggestion.

    Body: ``{"outcome": "accepted" | "rejected" | "ignored"}``.
    If accepted with an ``accept_service``, the service is called
    to take the suggested action.
    """
    from mylo.monitor.suggestions import record_outcome
    from mylo.server.app import AppKeys

    suggestion_id = request.match_info["suggestion_id"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    outcome = body.get("outcome")
    if outcome not in ("accepted", "rejected", "ignored"):
        return web.json_response(
            {"ok": False, "error": "outcome must be 'accepted', 'rejected', or 'ignored'"},
            status=400,
        )

    store = request.app[AppKeys.MEMORY]
    memory = store.current()
    record_outcome(memory, suggestion_id, outcome)

    # If accepted with a service call, execute it.
    if outcome == "accepted":
        service = body.get("service")
        target = body.get("target")
        if service and target:
            try:
                domain, svc = service.split(".", 1)
                ws_client = request.app[AppKeys.HA_CLIENT]
                await ws_client.send_command(
                    "call_service",
                    domain=domain,
                    service=svc,
                    target=target,
                    timeout=10.0,
                )
            except Exception as exc:
                log.warning("suggestion.service_call_failed", error=str(exc))

    await store.save(memory, note=f"suggestion {suggestion_id}: {outcome}")

    return web.json_response({"ok": True, "id": suggestion_id, "outcome": outcome})


async def _handle_suggestion_automated(request: web.Request) -> web.Response:
    """Mark a suggestion as automated after the user created an automation.

    Body: ``{"automation_id": "mylo_kitchen_off_when_away"}``.
    """
    from mylo.server.app import AppKeys

    suggestion_id = request.match_info["suggestion_id"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}

    automation_id = body.get("automation_id")

    store = request.app[AppKeys.MEMORY]
    memory = store.current()

    for s in memory.suggestions:
        if s.id == suggestion_id:
            s.automated = True
            s.automation_id = automation_id
            await store.save(
                memory, note=f"suggestion {suggestion_id} automated as {automation_id}"
            )
            return web.json_response({"ok": True, "id": suggestion_id, "automated": True})

    return web.json_response({"ok": False, "error": "suggestion_not_found"}, status=404)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _serialize_prune(report: PruneReport) -> list[dict[str, str]]:
    return [
        {
            "section": c.section,
            "id": c.item_id,
            "reason": c.reason,
            "summary": c.summary,
        }
        for c in report.candidates
    ]
