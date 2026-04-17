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
    app.router.add_get("/api/conversation", _handle_get_conversation)
    app.router.add_get("/api/health", _handle_health)
    app.router.add_get("/api/status", _handle_status)
    app.router.add_get("/api/activity", _handle_activity)


async def _handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


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
            1
            for e in registries.entities.values()
            if not e.disabled_by and not e.hidden_by
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

    return web.json_response(
        {
            "ok": True,
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
    )
    system_text = assembled.system

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
