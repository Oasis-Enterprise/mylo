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

"""Chat endpoints.

``POST /api/chat`` — submit a user message, receive a server-sent-events
stream of tool_loop events. Event types match the CLI:

* ``text`` — ``{"text": "..."}``
* ``tool_call`` — ``{"name": "...", "input": {...}, "id": "..."}``
* ``tool_result`` — ``{"name": "...", "status": "ok|error", "data": ...}``
* ``done`` — ``{"stop_reason": "...", "usage": {...}}``

SSE was chosen over websocket because: (a) HA Ingress proxies SSE cleanly,
(b) a single POST→stream is sufficient for our flow — no bidirectional
signaling needed in M5a.

``POST /api/conversation/clear`` resets the conversation — trivial helper
so the UI can offer a ``/clear`` button.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import web

from mylo.context.assembler import assemble_system_prompt
from mylo.context.memory_injection import render_current_time
from mylo.llm.tool_loop import (
    DoneEvent,
    LoopEvent,
    TextEvent,
    ToolCallEvent,
    ToolResultEvent,
    run_turn,
)
from mylo.logging_setup import get_logger

log = get_logger(__name__)


def register_chat_routes(app: web.Application) -> None:
    app.router.add_post("/api/chat", _handle_chat)
    app.router.add_post("/api/conversation/clear", _handle_clear)
    app.router.add_post("/api/conversation/new", _handle_new_conversation)
    app.router.add_get("/api/conversation", _handle_get_conversation)
    app.router.add_get("/api/catchup", _handle_catchup)
    app.router.add_get("/api/health", _handle_health)
    app.router.add_get("/api/status", _handle_status)
    app.router.add_get("/api/activity", _handle_activity)


async def _handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _handle_catchup(request: web.Request) -> web.Response:
    """Lightweight status summary for the catch-up banner.

    Returns what happened since the user's last interaction: memory
    sync results, background monitor findings, automation errors.
    Built from data we already have — no LLM call, no token cost.

    The UI shows this as a divider banner when the gap since the last
    message exceeds a threshold (default 2h).
    """
    from datetime import UTC, datetime

    from mylo.server.app import AppKeys

    conv = request.app[AppKeys.CONVERSATION]

    # Find the timestamp of the last message.
    last_ts: str | None = None
    if conv.history:
        for msg in reversed(conv.history):
            ts = msg.get("created_at")
            if isinstance(ts, str):
                last_ts = ts
                break

    if last_ts is None:
        # No conversation history — no gap to report.
        return web.json_response({"show_banner": False})

    try:
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except ValueError:
        return web.json_response({"show_banner": False})

    now = datetime.now(UTC)
    gap_seconds = (now - last_dt).total_seconds()
    gap_hours = gap_seconds / 3600

    # Only show banner for gaps > 2 hours.
    if gap_hours < 2:
        return web.json_response({"show_banner": False, "gap_hours": round(gap_hours, 1)})

    # Build summary from existing data sources.
    lines: list[str] = []

    # Memory changes since last interaction.
    memory_store = request.app.get(AppKeys.MEMORY)
    if memory_store is not None:
        mem = memory_store.current()
        if mem.last_sync and mem.last_sync > last_ts:
            lines.append("Memory sync ran — context updated")

    # Recent audit activity since last message.
    if AppKeys.TOOL_CONTEXT in request.app:
        audit = request.app[AppKeys.TOOL_CONTEXT].audit
        recent = audit.read_recent(limit=50)
        since_last = [e for e in recent if e.get("timestamp", "") > last_ts]
        if since_last:
            successes = sum(1 for e in since_last if e.get("result") == "success")
            failures = sum(1 for e in since_last if e.get("result") == "failure")
            if successes:
                lines.append(f"{successes} background action(s) completed")
            if failures:
                lines.append(f"{failures} action(s) failed — check Activity tab")

    # Pending actions from the proactive suggestion engine.
    pending_actions: list[dict[str, Any]] = []
    if memory_store is not None:
        mem = memory_store.current()
        for pa in mem.pending_actions:
            if not pa.resolved:
                lines.append(pa.message)
                pending_actions.append(
                    {
                        "id": pa.id,
                        "type": pa.type,
                        "entity_id": pa.entity_id,
                        "title": pa.title,
                        "message": pa.message,
                        "detected_at": pa.detected_at,
                    }
                )

    if not lines:
        lines.append("No new activity while you were away")

    # Format the gap duration.
    if gap_hours < 24:
        gap_label = f"{int(gap_hours)} hours"
    else:
        days = int(gap_hours / 24)
        gap_label = f"{days} day{'s' if days != 1 else ''}"

    return web.json_response(
        {
            "show_banner": True,
            "gap_label": f"{gap_label} since last message",
            "lines": lines,
            "pending_actions": pending_actions,
        }
    )


async def _handle_activity(request: web.Request) -> web.Response:
    """Paginated audit log for the Activity tab.

    Query params:
    - ``limit`` (int, default 100): max entries to return.
    - ``tool`` (str): filter to a specific tool name.
    - ``result`` (str): filter to a result kind (success/failure/rolled_back/denied).
    - ``tier`` (int): filter to a tier level.
    """
    from mylo.server.app import AppKeys

    audit = AppKeys.TOOL_CONTEXT in request.app and request.app[AppKeys.TOOL_CONTEXT].audit

    if not audit:
        return web.json_response({"entries": [], "total": 0})

    limit = int(request.query.get("limit", "200"))
    tool_filter = request.query.get("tool")
    result_filter = request.query.get("result")
    tier_filter = request.query.get("tier")

    entries = request.app[AppKeys.TOOL_CONTEXT].audit.read_recent(limit=limit)

    if tool_filter:
        entries = [e for e in entries if e.get("tool_name") == tool_filter]
    if result_filter:
        entries = [e for e in entries if e.get("result") == result_filter]
    if tier_filter:
        try:
            tier_val = int(tier_filter)
            entries = [e for e in entries if e.get("tier") == tier_val]
        except ValueError:
            pass

    return web.json_response({"entries": entries, "total": len(entries)})


async def _handle_status(request: web.Request) -> web.Response:
    """Header status-bar payload — entity/automation counts, memory
    sync freshness, version.

    The panel header polls this on mount so we avoid stuffing all of
    this into the SSE stream. Cheap: every field is already cached in
    memory (registries + MemoryStore.current()).
    """
    from mylo.server.app import AppKeys

    registries = request.app.get(AppKeys.REGISTRIES)
    memory_store = request.app.get(AppKeys.MEMORY)

    entity_count = 0
    automation_count = 0
    if registries is not None:
        entity_count = sum(
            1 for e in registries.entities.values() if not e.disabled_by and not e.hidden_by
        )
        automation_count = sum(
            1
            for e in registries.entities.values()
            if e.domain == "automation" and not e.disabled_by and not e.hidden_by
        )

    memory_payload: dict[str, Any] = {"last_sync": None, "pending_conflicts": 0}
    if memory_store is not None:
        mem = memory_store.current()
        memory_payload = {
            "last_sync": mem.last_sync,
            "pending_conflicts": len(mem.pending_conflicts()),
        }

    from mylo import __version__

    return web.json_response(
        {
            "ok": True,
            "version": __version__,
            "entities": entity_count,
            "automations": automation_count,
            "memory": memory_payload,
            "has_provider": AppKeys.PROVIDER in request.app,
        }
    )


async def _handle_clear(request: web.Request) -> web.Response:
    from mylo.server.app import AppKeys

    conv = request.app[AppKeys.CONVERSATION]
    await conv.clear()
    return web.json_response({"ok": True})


async def _handle_new_conversation(request: web.Request) -> web.Response:
    """Archive the current conversation and start fresh.

    The old messages stay in SQLite under the old conversation_id.
    A new conversation_id is generated so the LLM context starts
    clean. The UI should clear its local items after this returns.
    """
    from datetime import UTC, datetime

    from mylo.server.app import AppKeys

    conv = request.app[AppKeys.CONVERSATION]
    base_ctx = request.app[AppKeys.TOOL_CONTEXT]

    old_id = conv.conversation_id
    new_id = f"conv_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

    conv.conversation_id = new_id
    conv.history = []
    base_ctx.conversation_id = new_id

    log.info("conversation.new", old_id=old_id, new_id=new_id)
    return web.json_response({"ok": True, "old_id": old_id, "new_id": new_id})


async def _handle_get_conversation(request: web.Request) -> web.Response:
    """Return the current conversation history.

    Used by the UI as a polling fallback: if the SSE stream dies
    mid-turn (which happens when a tier-2 write triggers reload_all
    and HA's ingress restarts), the UI can poll this endpoint and
    render any turns that finished after the disconnect.
    """
    from mylo.server.app import AppKeys

    conv = request.app[AppKeys.CONVERSATION]
    # Freshly read from storage in case a background turn completed
    # while the UI was disconnected.
    await conv.load(limit=int(os.environ.get("MYLO_HISTORY_LIMIT", "12")))
    return web.json_response({"messages": conv.history})


async def _handle_chat(request: web.Request) -> web.StreamResponse:
    from mylo.server.app import AppKeys

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "missing_message"}, status=400)

    # The UI sets ``approved: true`` when the user clicks "Apply" after
    # seeing a dry-run preview. That flag travels on the next request,
    # authorizing tier-2/3 writes for this turn only. Default false.
    approved = bool(body.get("approved", False))
    # The UI passes the running session cost so the assembler can inject
    # a budget warning when we're approaching the configured cap.
    session_cost = float(body.get("session_cost_usd", 0.0))

    provider = request.app.get(AppKeys.PROVIDER)
    if provider is None:
        return web.json_response(
            {"error": "no_api_key", "message": "ANTHROPIC_API_KEY is not set"},
            status=503,
        )

    conv = request.app[AppKeys.CONVERSATION]
    base_ctx = request.app[AppKeys.TOOL_CONTEXT]
    # Per-turn context — user_approved is request-scoped, not server-wide.
    from mylo.tools.context import ToolContext

    ctx = ToolContext(
        ws_client=base_ctx.ws_client,
        registries=base_ctx.registries,
        config=base_ctx.config,
        permissions=base_ctx.permissions,
        audit=base_ctx.audit,
        states=base_ctx.states,
        conversation_id=base_ctx.conversation_id,
        user_approved=approved,
        dry_run=False,
    )
    tools = request.app[AppKeys.TOOLS_JSON]
    config = request.app[AppKeys.CONFIG]

    # Full four-layer system prompt. The assembler reads live registries
    # + memory + the latest user turn and picks which memory sections /
    # task references to include. Scratchpad is re-read each turn so
    # notes the user just recorded are available immediately.
    memory_store = request.app[AppKeys.MEMORY]
    assembled = assemble_system_prompt(
        registries=request.app.get(AppKeys.REGISTRIES),
        memory=memory_store.current(),
        conversation_text=message,
        mylo_data_dir=config.mylo_data_dir,
        timezone=request.app.get(AppKeys.HA_TIMEZONE),
        session_cost_usd=session_cost,
        session_budget_usd=config.session_budget_usd,
        is_local_provider=config.llm_provider == "ollama",
    )
    system_text = assembled.system

    # Prefix the user message with a compact timestamp so the model
    # knows the current time without it being in the system prompt.
    # This keeps the system block stable across turns, enabling
    # Anthropic's prompt cache (~5,500 tokens saved per cache hit).
    time_prefix = render_current_time(request.app.get(AppKeys.HA_TIMEZONE))
    message = f"[{time_prefix}] {message}"

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # hint for proxies (nginx/ingress)
        },
    )
    await response.prepare(request)

    async def emit(name: str, data: dict[str, Any]) -> None:
        payload = f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"
        await response.write(payload.encode("utf-8"))

    try:
        async for event in _turn_events(
            message=message,
            conversation=conv,
            provider=provider,
            ctx=ctx,
            system=system_text,
            tools=tools,
            model=config.model,
            prompt_version=assembled.prompt_version,
        ):
            if isinstance(event, TextEvent):
                await emit("text", {"text": event.text})
            elif isinstance(event, ToolCallEvent):
                await emit(
                    "tool_call",
                    {"id": event.id, "name": event.name, "input": event.input},
                )
            elif isinstance(event, ToolResultEvent):
                await emit(
                    "tool_result",
                    {
                        "id": event.id,
                        "name": event.name,
                        "status": event.status,
                        "error_code": event.error_code,
                        "data": event.data,
                    },
                )
            elif isinstance(event, DoneEvent):
                await emit(
                    "done",
                    {"stop_reason": event.stop_reason, "usage": event.usage},
                )
    except Exception as exc:
        log.exception("chat.turn_failed")
        await emit("error", {"message": str(exc), "type": type(exc).__name__})
    finally:
        await response.write_eof()
    return response


async def _turn_events(**kwargs: Any) -> AsyncIterator[LoopEvent]:
    async for event in run_turn(user_message=kwargs.pop("message"), **kwargs):
        yield event
