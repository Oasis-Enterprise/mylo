"""The agent's turn-taking loop.

Given a provider, a conversation, a system prompt, and a tool context,
:func:`run_turn` yields a stream of typed events:

* :class:`TextEvent` — something the model said.
* :class:`ToolCallEvent` — about to execute a tool.
* :class:`ToolResultEvent` — tool finished with this result.
* :class:`DoneEvent` — turn over.

The loop stops when the model's ``stop_reason`` is not ``tool_use``, or
after ``max_iterations`` safety rounds. Anthropic occasionally emits empty
turns; the bound prevents runaway loops on misbehaving models.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from mylo.conversation.manager import ConversationManager
from mylo.llm.provider import Provider, ProviderMessage, ToolCall
from mylo.logging_setup import get_logger
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute

log = get_logger(__name__)


# ─── Events ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class TextEvent:
    text: str


@dataclass(slots=True)
class ToolCallEvent:
    name: str
    input: dict[str, Any]
    id: str


@dataclass(slots=True)
class ToolResultEvent:
    name: str
    id: str
    status: str  # "ok" | "error"
    data: Any
    error_code: str | None = None


@dataclass(slots=True)
class DoneEvent:
    stop_reason: str
    usage: dict[str, int]


# Union type alias for consumers.
LoopEvent = TextEvent | ToolCallEvent | ToolResultEvent | DoneEvent


# ─── Loop ────────────────────────────────────────────────────────────────────


async def run_turn(
    *,
    user_message: str,
    conversation: ConversationManager,
    provider: Provider,
    ctx: ToolContext,
    system: str,
    tools: list[dict[str, Any]],
    model: str,
    max_iterations: int = 8,
    prompt_version: str | None = None,
) -> AsyncIterator[LoopEvent]:
    """Run one user-initiated turn to completion.

    Appends the user message, then loops: call the model, emit text +
    tool calls, execute each tool, feed results back, call the model
    again. Stops when the model declares end of turn.
    """
    await conversation.append("user", user_message, prompt_version=prompt_version)

    usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    stop_reason = ""

    for _iteration in range(max_iterations):
        messages: list[ProviderMessage] = conversation.as_provider_messages()
        response = await provider.message(
            system=system,
            messages=messages,
            tools=tools,
            model=model,
        )

        for key, value in response.usage.items():
            usage_total[key] = usage_total.get(key, 0) + value
        stop_reason = response.stop_reason

        # Persist the assistant turn exactly as the provider returned it —
        # content blocks intact so the next call can replay them.
        await conversation.append(
            "assistant", response.content_blocks, prompt_version=prompt_version
        )

        if response.text:
            yield TextEvent(text=response.text)

        if not response.tool_calls:
            break

        tool_results = await _execute_tools(response.tool_calls, ctx)
        for call, envelope in zip(response.tool_calls, tool_results, strict=True):
            yield ToolCallEvent(name=call.name, input=call.input, id=call.id)
            yield ToolResultEvent(
                name=call.name,
                id=call.id,
                status=envelope["status"],
                data=envelope.get("data"),
                error_code=(envelope.get("error") or {}).get("code"),
            )

        # Feed the tool results back as a user turn with tool_result blocks.
        result_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps(envelope, default=str),
            }
            for call, envelope in zip(response.tool_calls, tool_results, strict=True)
        ]
        await conversation.append("user", result_blocks, prompt_version=prompt_version)
    else:
        log.warning("llm.tool_loop.max_iterations_hit", iterations=max_iterations)

    yield DoneEvent(stop_reason=stop_reason, usage=usage_total)


async def _execute_tools(calls: list[ToolCall], ctx: ToolContext) -> list[dict[str, Any]]:
    """Run each tool call in order through the executor and return the
    list of ToolResult.to_dict() envelopes. Serial on purpose — concurrent
    mutation would complicate audit ordering and the model doesn't benefit.
    """
    out: list[dict[str, Any]] = []
    for call in calls:
        result = await execute(call.name, call.input, ctx)
        out.append(result.to_dict())
    return out
