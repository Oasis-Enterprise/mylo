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
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import web

from mylo.context.basic_prompt import load_system_prompt
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
    app.router.add_get("/api/health", _handle_health)


async def _handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _handle_clear(request: web.Request) -> web.Response:
    from mylo.server.app import AppKeys

    conv = request.app[AppKeys.CONVERSATION]
    await conv.clear()
    return web.json_response({"ok": True})


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
    prompt = load_system_prompt()

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
            system=prompt.text,
            tools=tools,
            model=config.model,
            prompt_version=prompt.version,
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
