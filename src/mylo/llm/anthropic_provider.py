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

from typing import Any

from anthropic import AsyncAnthropic

from mylo.llm.provider import ProviderMessage, ProviderResponse, ToolCall


class AnthropicProvider:
    def __init__(self, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)

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

        response = await self._client.messages.create(
            model=model,
            system=system_blocks,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
            tools=cached_tools,  # type: ignore[arg-type]
            max_tokens=max_tokens,
        )

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
