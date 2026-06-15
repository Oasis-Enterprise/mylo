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
from mylo.conversation.summarize import compress_old_tool_results
from mylo.llm.provider import Provider, ProviderMessage
from mylo.logging_setup import get_logger
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute

log = get_logger(__name__)

# History is append-only within a turn so the prompt cache stays warm.
# This ceiling is a backstop that forces compaction only if a single turn
# grows toward the model's context window — well above any normal turn.
_HISTORY_SAFETY_CEILING_TOKENS = 150_000


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
    # True when the turn ended because it hit max_iterations while the
    # model still wanted to use tools — i.e. it was cut off mid-task, not
    # naturally finished. Lets the UI distinguish "paused" from "done".
    truncated: bool = False


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
    max_iterations: int = 25,
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
    truncated = False

    for _iteration in range(max_iterations):
        messages: list[ProviderMessage] = conversation.as_provider_messages()

        # Safety ceiling: history is append-only within a turn (cheap via
        # prompt caching), but a pathological turn could still approach the
        # model's context window. Only then do we force compaction. This is
        # a backstop, not the routine path.
        history_chars = sum(
            len(json.dumps(m["content"], default=str))
            if not isinstance(m["content"], str)
            else len(m["content"])
            for m in messages
        )
        estimated_tokens = history_chars // 4  # rough char-to-token ratio
        if estimated_tokens > _HISTORY_SAFETY_CEILING_TOKENS:
            log.info(
                "llm.history_ceiling_hit",
                estimated_tokens=estimated_tokens,
                compressing=True,
            )
            conversation.history = compress_old_tool_results(
                conversation.history, keep_last_n_turns=1
            )
            messages = conversation.as_provider_messages()

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

        # Yield one TextEvent per text block — preserves exact provider
        # output without risk of truncation or reordering from a join.
        for block in response.content_blocks:
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    yield TextEvent(text=text)

        if not response.tool_calls:
            break

        # Emit each ToolCallEvent *before* executing, so the user sees what
        # the agent is reaching for during slow tool runs. Emit the
        # ToolResultEvent immediately after each call completes.
        tool_results: list[dict[str, Any]] = []
        for call in response.tool_calls:
            yield ToolCallEvent(name=call.name, input=call.input, id=call.id)
            envelope = (await execute(call.name, call.input, ctx)).to_dict()
            tool_results.append(envelope)
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

        # NB: history is intentionally append-only WITHIN a turn now. We do
        # NOT compress between iterations — doing so dropped the entity_ids
        # the model had just gathered (forcing endless re-queries) and
        # rewrote earlier bytes every round, defeating the prompt cache.
        # Keeping the prefix stable lets each iteration's prior history be
        # served from cache (~90% off). Between-turn compression still runs
        # at the end of the loop. The safety ceiling below is the only
        # mid-turn compaction, and only on a genuinely runaway turn.
    else:
        # Loop exhausted while the model still wanted tools — it was cut
        # off mid-task. Surface that explicitly so the paused turn doesn't
        # look like a completed one (otherwise the user has to guess and
        # type "keep going").
        log.warning("llm.tool_loop.max_iterations_hit", iterations=max_iterations)
        truncated = True
        yield TextEvent(
            text=(
                f"I paused after {max_iterations} steps to check in — there's more to "
                'do here. Reply "continue" and I\'ll pick up where I left off.'
            )
        )

    # Final compression with the standard window for between-turn savings.
    conversation.history = compress_old_tool_results(conversation.history)

    yield DoneEvent(stop_reason=stop_reason, usage=usage_total, truncated=truncated)
