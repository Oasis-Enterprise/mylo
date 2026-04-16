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

log = get_logger(__name__)


def register_memory_routes(app: web.Application) -> None:
    app.router.add_get("/api/memory", _handle_get_memory)
    app.router.add_get("/api/memory/full", _handle_get_full)
    app.router.add_post("/api/memory/sync", _handle_sync)
    app.router.add_post("/api/memory/prune", _handle_prune)
    app.router.add_delete("/api/memory/item", _handle_delete_item)
    app.router.add_post("/api/memory/conflict/{conflict_id}/resolve", _handle_resolve_conflict)


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
    if result.updated is not None:
        memory_to_save = result.updated
        if should_apply_prune and result.prune_report.total > 0:
            memory_to_save = apply_prune(memory_to_save, result.prune_report)
            applied = True
        await store.save(memory_to_save, note=f"sync: {result.summary}")
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
        return web.json_response(
            {"ok": True, "applied": 0, "prune_candidates": []}
        )

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
