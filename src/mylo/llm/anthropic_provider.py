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

"""Anthropic Provider implementation.

Uses the async SDK's non-streaming ``messages.create``. Streaming is a
polish concern for the UI (M5); the CLI doesn't need it yet, and avoiding
streaming keeps the tool loop a straightforward sequence of turns.

Applies Anthropic's prompt caching on the system prompt and the tool
block:

* System prompt goes in as a single block with
  ``cache_control: {"type": "ephemeral"}`` → Anthropic caches it for 5
  minutes after first use.
* The last tool definition carries ``cache_control`` → everything up to
  and including that tool is cached.

First call in a 5-minute window pays the full input price (plus a small
cache-write surcharge). Every follow-up call that matches the cache reads
the cached prefix at ~10% of normal cost and doesn't count against the
per-minute token budget. This is the single biggest dev-ergonomics win:
repeated questions in the same session stop burning tokens on the same
~7KB of tool schemas.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic

from mylo.llm.provider import ProviderMessage, ProviderResponse, ToolCall
from mylo.logging_setup import get_logger

log = get_logger(__name__)


def _with_history_cache_breakpoint(
    messages: list[ProviderMessage],
) -> list[ProviderMessage]:
    """Return a copy of ``messages`` with an ephemeral cache breakpoint on
    the last content block of the last message.

    Copy-on-write: the message dict, its content list, and the annotated
    block are all copied, so the caller's history (which persists to disk)
    is never mutated. A string content is promoted to a single text block.
    Returns the input unchanged if there's nothing annotatable.
    """
    if not messages:
        return messages

    src = messages[-1]
    content = src.get("content")
    new_content: list[dict[str, Any]]

    if isinstance(content, str):
        if not content:
            return messages
        new_content = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        new_content = list(content)
        tail = dict(new_content[-1])
        tail["cache_control"] = {"type": "ephemeral"}
        new_content[-1] = tail
    else:
        return messages

    out = list(messages)
    out[-1] = {"role": src["role"], "content": new_content}
    return out


class AnthropicProvider:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = AsyncAnthropic(api_key=api_key)

    def _rebuild_client(self) -> None:
        """Replace the underlying client — drops its connection pool.

        httpx keeps connections alive between requests. If one goes half-
        open (remote sent FIN but we didn't notice until the next send),
        the pool will hand out a dead socket. A fresh client forces new
        connections on the next call.
        """
        self._client = AsyncAnthropic(api_key=self._api_key)

    async def message(
        self,
        *,
        system: str,
        messages: list[ProviderMessage],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int = 4096,
    ) -> ProviderResponse:
        # Cache the system prompt.
        system_blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Cache through all tools: add cache_control to the last one. The
        # provider caches the prefix up to and including that block.
        cached_tools: list[dict[str, Any]] = []
        for i, tool in enumerate(tools):
            entry = dict(tool)
            if i == len(tools) - 1:
                entry["cache_control"] = {"type": "ephemeral"}
            cached_tools.append(entry)

        # Cache the conversation history too: a moving breakpoint on the
        # last block. Within an agentic turn the history is append-only, so
        # each iteration reads the prior history from cache (~90% off) and
        # only writes its new tail. Copy-on-write so we never mutate the
        # caller's history (it flushes to SQLite).
        cached_messages = _with_history_cache_breakpoint(messages)

        async def _call() -> Any:
            return await self._client.messages.create(
                model=model,
                system=system_blocks,  # type: ignore[arg-type]
                messages=cached_messages,  # type: ignore[arg-type]
                tools=cached_tools,  # type: ignore[arg-type]
                max_tokens=max_tokens,
            )

        _BACKOFF_DELAYS = (2.0, 5.0, 15.0)

        for attempt in range(len(_BACKOFF_DELAYS) + 1):
            try:
                response = await _call()
                break
            except APIConnectionError:
                log.warning("anthropic.connection_error_rebuilding_pool")
                self._rebuild_client()
                response = await _call()
                break
            except APIStatusError as exc:
                # 429 (rate limit) and 5xx — including 529 "overloaded",
                # which Anthropic returns during platform-wide load spikes
                # — are transient. Back off and retry. Other 4xx are
                # caller errors (bad request, auth, etc.); re-raise.
                if exc.status_code != 429 and exc.status_code < 500:
                    raise
                if attempt >= len(_BACKOFF_DELAYS):
                    log.error(
                        "anthropic.transient_error_exhausted_retries",
                        status_code=exc.status_code,
                    )
                    raise
                delay = _BACKOFF_DELAYS[attempt] + random.uniform(0, 1)
                log.warning(
                    "anthropic.transient_error",
                    status_code=exc.status_code,
                    attempt=attempt + 1,
                    backoff_seconds=round(delay, 1),
                )
                await asyncio.sleep(delay)

        content_blocks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            as_dict = block.model_dump()
            content_blocks.append(as_dict)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                raw_input = block.input if isinstance(block.input, dict) else {}
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=raw_input))

        usage: dict[str, int] = {}
        if response.usage is not None:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
            # Cache-specific counters if reported.
            if getattr(response.usage, "cache_creation_input_tokens", None) is not None:
                usage["cache_creation_input_tokens"] = (
                    response.usage.cache_creation_input_tokens or 0
                )
            if getattr(response.usage, "cache_read_input_tokens", None) is not None:
                usage["cache_read_input_tokens"] = response.usage.cache_read_input_tokens or 0

        return ProviderResponse(
            content_blocks=content_blocks,
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "",
            usage=usage,
        )
