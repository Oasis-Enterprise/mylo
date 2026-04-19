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

"""OpenAI Provider implementation.

Uses the async ``openai`` SDK's ``chat.completions.create`` with tool
calling. Maps between Anthropic's native content-block format (which
the rest of Mylo uses) and OpenAI's message/tool-call format.

OpenAI differences from Anthropic that matter here:

* System prompt is a ``{"role": "system", "content": text}`` message,
  not a top-level ``system`` kwarg.
* Tool definitions wrap in ``{"type": "function", "function": {...}}``.
* Tool calls come back as ``message.tool_calls`` (list), not as
  content blocks. Each has ``id``, ``function.name``, ``function.arguments``.
* Tool results go back as ``{"role": "tool", "tool_call_id": ...,
  "content": ...}`` messages — not nested inside a user message.
* ``stop_reason`` is ``finish_reason`` with values ``stop`` / ``tool_calls``
  instead of ``end_turn`` / ``tool_use``.
"""

from __future__ import annotations

import json
from typing import Any

from openai import APIConnectionError, AsyncOpenAI

from mylo.llm.provider import ProviderMessage, ProviderResponse, ToolCall
from mylo.logging_setup import get_logger

log = get_logger(__name__)


class OpenAIProvider:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._default_model = default_model
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    async def message(
        self,
        *,
        system: str,
        messages: list[ProviderMessage],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int = 4096,
    ) -> ProviderResponse:
        oai_messages = _convert_messages(system, messages)
        oai_tools = _convert_tools(tools)

        kwargs: dict[str, Any] = {
            "model": self._default_model or model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except APIConnectionError:
            log.warning("openai.connection_error_retrying")
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
            )
            response = await self._client.chat.completions.create(**kwargs)

        choice = response.choices[0] if response.choices else None
        if choice is None:
            return ProviderResponse(
                content_blocks=[],
                text="",
                stop_reason="error",
                usage={},
            )

        msg = choice.message
        content_blocks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        if msg.content:
            content_blocks.append({"type": "text", "text": msg.content})
            text_parts.append(msg.content)

        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": args,
                }
            )
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=args))

        # Map OpenAI finish_reason to Anthropic stop_reason.
        stop_reason = "end_turn"
        if choice.finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif choice.finish_reason == "length":
            stop_reason = "max_tokens"

        usage: dict[str, int] = {}
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }

        return ProviderResponse(
            content_blocks=content_blocks,
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
        )


def _convert_messages(system: str, messages: list[ProviderMessage]) -> list[dict[str, Any]]:
    """Convert Anthropic-shaped messages to OpenAI format.

    Key differences:
    - System prompt is a message with role=system
    - tool_result blocks become separate role=tool messages
    - tool_use blocks in assistant messages become tool_calls
    """
    oai: list[dict[str, Any]] = [{"role": "system", "content": system}]

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            oai.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            oai.append({"role": role, "content": str(content)})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls_list: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls_list.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        }
                    )
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                assistant_msg["content"] = "\n".join(text_parts)
            else:
                assistant_msg["content"] = None
            if tool_calls_list:
                assistant_msg["tool_calls"] = tool_calls_list
            oai.append(assistant_msg)

        elif role == "user":
            # User messages may contain tool_result blocks (from the
            # tool loop) mixed with text. Split them out.
            text_parts_user: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    oai.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": block.get("content", ""),
                        }
                    )
                elif block.get("type") == "text":
                    text_parts_user.append(block.get("text", ""))
            if text_parts_user:
                oai.append({"role": "user", "content": "\n".join(text_parts_user)})

    return oai


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool schema to OpenAI function format."""
    oai_tools: list[dict[str, Any]] = []
    for tool in tools:
        oai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )
    return oai_tools
