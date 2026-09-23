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
* :class:`TextDeltaEvent` — a chunk of assistant text while streaming.
* :class:`StatusEvent` — what the loop is doing right now (thinking / tool /
  cancelled), for a live status panel.
* :class:`ToolCallEvent` — about to execute a tool.
* :class:`ToolResultEvent` — tool finished with this result.
* :class:`DoneEvent` — turn over.

The loop stops when the model's ``stop_reason`` is not ``tool_use``, or
after ``max_iterations`` safety rounds. Anthropic occasionally emits empty
turns; the bound prevents runaway loops on misbehaving models.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

from mylo.conversation.manager import ConversationManager
from mylo.conversation.summarize import compress_old_tool_results
from mylo.llm.cost import cache_hit_ratio, estimate_usd
from mylo.llm.provider import Provider, ProviderMessage, ProviderResponse, StreamDelta
from mylo.llm.status_labels import CANCELLED_LABEL, THINKING_LABEL, label_for
from mylo.logging_setup import get_logger
from mylo.tools.base import Tier
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute
from mylo.tools.registry import get as _get_tool_def

log = get_logger(__name__)

# History is append-only within a turn so the prompt cache stays warm.
# This ceiling is a backstop that forces compaction only if a single turn
# grows toward the model's context window — well above any normal turn.
_HISTORY_SAFETY_CEILING_TOKENS = 150_000

# Loop-guard: after this many identical repeats of a read call within a
# turn, nudge the model to stop re-querying and use what it already has.
_REPEAT_NUDGE_THRESHOLD = 2


def _read_call_key(name: str, tool_input: dict[str, Any]) -> str | None:
    """Stable dedup key for an identical READ tool call, or None for
    non-read tools (writes/actions are never deduped)."""
    tool_def = _get_tool_def(name)
    if tool_def is None or tool_def.tier != Tier.READ or not tool_def.cacheable:
        return None
    return f"{name}:{json.dumps(tool_input, sort_keys=True, default=str)}"


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


@dataclass(slots=True)
class TextDeltaEvent:
    """A chunk of assistant text while the model call is still running.
    The full text arrives afterwards as :class:`TextEvent`; consumers that
    paint deltas replace them with that final text."""

    text: str


@dataclass(slots=True)
class StatusEvent:
    """What the loop is doing right now, in plain words for the panel.

    ``phase`` is ``"thinking"`` (about to call the model), ``"tool"``
    (about to run ``tool``), or ``"cancelled"`` (the loop honoured a stop
    request and is ending the turn).
    """

    phase: str
    tool: str | None
    label: str


# Union type alias for consumers.
LoopEvent = TextEvent | TextDeltaEvent | StatusEvent | ToolCallEvent | ToolResultEvent | DoneEvent

# Assistant text persisted when a turn is stopped before any text arrived,
# so history keeps its user/assistant alternation and the panel has
# something to show.
STOPPED_TEXT = "Stopped."


class _Sentinel:
    pass


_CANCELLED = _Sentinel()
_EXHAUSTED = _Sentinel()


async def _provider_items(
    provider: Provider,
    **kwargs: Any,
) -> AsyncGenerator[StreamDelta | ProviderResponse, None]:
    """Drive ``provider.stream`` when it exists, else wrap ``message``."""
    stream_fn = getattr(provider, "stream", None)
    if stream_fn is None:
        yield await provider.message(**kwargs)
        return
    async for item in stream_fn(**kwargs):
        yield item


async def _next_or_cancel(
    items: AsyncGenerator[StreamDelta | ProviderResponse, None],
    cancel: asyncio.Event | None,
) -> StreamDelta | ProviderResponse | _Sentinel:
    """Await the next provider item, or ``_CANCELLED`` if ``cancel`` fires
    first (the pending fetch is cancelled, which closes the HTTP stream)."""
    next_task = asyncio.ensure_future(anext(items))
    if cancel is None:
        try:
            return await next_task
        except StopAsyncIteration:
            return _EXHAUSTED
    cancel_task = asyncio.ensure_future(cancel.wait())
    done, _pending = await asyncio.wait(
        {next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if next_task in done:
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        try:
            return next_task.result()
        except StopAsyncIteration:
            return _EXHAUSTED
    next_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
        await next_task
    return _CANCELLED


async def _persist_stop(
    conversation: ConversationManager,
    partial: list[str],
    prompt_version: str | None,
) -> list[LoopEvent]:
    """Append the stopped turn's assistant text and return the events that
    tell consumers about it (final text, then the cancelled status)."""
    text = "".join(partial) or STOPPED_TEXT
    await conversation.append(
        "assistant", [{"type": "text", "text": text}], prompt_version=prompt_version
    )
    return [TextEvent(text=text), StatusEvent(phase="cancelled", tool=None, label=CANCELLED_LABEL)]


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
    cancel: asyncio.Event | None = None,
) -> AsyncIterator[LoopEvent]:
    """Run one user-initiated turn to completion.

    Appends the user message, then loops: call the model, emit text +
    tool calls, execute each tool, feed results back, call the model
    again. Stops when the model declares end of turn.

    cancel — when set, the loop stops before the next model call or after
    the current tool batch; a model call in flight is aborted and its
    partial text kept. Tools are never interrupted.
    """
    await conversation.append("user", user_message, prompt_version=prompt_version)

    usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    stop_reason = ""
    truncated = False

    # Per-turn loop-guard: identical read calls are served from here
    # instead of re-executing, and repeats past a threshold trigger a nudge.
    seen_reads: dict[str, dict[str, Any]] = {}
    repeat_counts: dict[str, int] = {}

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

        # Defensive: trim/repair can yield an empty list if the stored
        # history is in a broken state (e.g. corrupted after a conversation
        # reset). Anthropic 400s on an empty messages array; fall back to
        # the user's current turn so the request still goes through.
        if not messages:
            log.warning("llm.empty_messages_fallback")
            messages = [ProviderMessage(role="user", content=user_message)]

        yield StatusEvent(phase="thinking", tool=None, label=THINKING_LABEL)
        if cancel is not None and cancel.is_set():
            for ev in await _persist_stop(conversation, [], prompt_version):
                yield ev
            stop_reason = "cancelled"
            break

        partial: list[str] = []
        response: ProviderResponse | None = None
        items = _provider_items(
            provider, system=system, messages=messages, tools=tools, model=model
        )
        try:
            while True:
                item = await _next_or_cancel(items, cancel)
                if isinstance(item, _Sentinel):
                    break
                if isinstance(item, StreamDelta):
                    partial.append(item.text)
                    yield TextDeltaEvent(text=item.text)
                else:
                    response = item
        except Exception:
            # The stream died after (maybe) some text reached the panel.
            # Keep what we have so history stays a valid alternation, then
            # let the route report the error.
            if partial:
                await conversation.append(
                    "assistant",
                    [{"type": "text", "text": "".join(partial)}],
                    prompt_version=prompt_version,
                )
            raise
        finally:
            await items.aclose()

        if response is None:
            if cancel is not None and cancel.is_set():
                for ev in await _persist_stop(conversation, partial, prompt_version):
                    yield ev
                stop_reason = "cancelled"
                break
            raise RuntimeError("provider stream ended without a final response")

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
        nudge_repeat = False
        for call in response.tool_calls:
            yield ToolCallEvent(name=call.name, input=call.input, id=call.id)
            yield StatusEvent(phase="tool", tool=call.name, label=label_for(call.name))
            read_key = _read_call_key(call.name, call.input)
            if read_key is not None and read_key in seen_reads:
                # Identical read already run this turn — reuse it, don't
                # re-execute. Count the repeat; nudge once it persists.
                envelope = seen_reads[read_key]
                repeat_counts[read_key] = repeat_counts.get(read_key, 0) + 1
                if repeat_counts[read_key] >= _REPEAT_NUDGE_THRESHOLD:
                    nudge_repeat = True
                log.info(
                    "llm.tool_loop.dedup_read", tool=call.name, repeats=repeat_counts[read_key]
                )
            else:
                envelope = (await execute(call.name, call.input, ctx)).to_dict()
                if read_key is not None and envelope.get("status") == "ok":
                    seen_reads[read_key] = envelope
            tool_results.append(envelope)
            yield ToolResultEvent(
                name=call.name,
                id=call.id,
                status=envelope["status"],
                data=envelope.get("data"),
                error_code=(envelope.get("error") or {}).get("code"),
            )

        # Feed the tool results back as a user turn with tool_result blocks.
        result_blocks: list[dict[str, Any]] = [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps(envelope, default=str),
            }
            for call, envelope in zip(response.tool_calls, tool_results, strict=True)
        ]
        if nudge_repeat:
            result_blocks.append(
                {
                    "type": "text",
                    "text": (
                        "You've already fetched this exact data earlier in this "
                        "turn and it hasn't changed. Use the results you already "
                        "have (entity ids are included) and proceed — do not "
                        "query the same thing again."
                    ),
                }
            )
        await conversation.append("user", result_blocks, prompt_version=prompt_version)

        if cancel is not None and cancel.is_set():
            for ev in await _persist_stop(conversation, [], prompt_version):
                yield ev
            stop_reason = "cancelled"
            break

        # ask_user pauses the turn HERE — after the tool_result is
        # persisted (the next turn's history and UI hydration need it),
        # before another provider round. Enforcing the pause in the loop
        # means the model structurally cannot answer its own question;
        # the user's reply arrives as the next chat message.
        if any((env.get("data") or {}).get("await_user_input") for env in tool_results):
            stop_reason = "awaiting_user_input"
            break

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

    # Per-turn telemetry — measures the caching/dedup win across iterations.
    log.info(
        "llm.turn_usage",
        model=model,
        input_tokens=usage_total.get("input_tokens", 0),
        output_tokens=usage_total.get("output_tokens", 0),
        cache_read_tokens=usage_total.get("cache_read_input_tokens", 0),
        cache_write_tokens=usage_total.get("cache_creation_input_tokens", 0),
        cache_hit_ratio=round(cache_hit_ratio(usage_total), 3),
        estimated_usd=round(estimate_usd(usage_total, model), 4),
        truncated=truncated,
    )

    yield DoneEvent(stop_reason=stop_reason, usage=usage_total, truncated=truncated)
