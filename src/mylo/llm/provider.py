"""LLM provider abstraction.

The tool loop (:mod:`mylo.llm.tool_loop`) talks to this interface, not to
any specific SDK. The Anthropic implementation is in
:mod:`mylo.llm.anthropic_provider`; future OpenAI / Ollama providers
slot in behind the same Protocol.

Message shape matches Anthropic's native format (role + content blocks)
because it's the most expressive — text, tool_use, and tool_result blocks
in a single list. The OpenAI adapter (when it lands) will translate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict

Role = Literal["user", "assistant"]


class ProviderMessage(TypedDict):
    """A message in the provider-facing conversation.

    ``content`` is either a plain string (user turns pre-tool-use) or a
    list of content blocks (assistant turns with tool_use, user turns with
    tool_result).
    """

    role: Role
    content: str | list[dict[str, Any]]


@dataclass(slots=True)
class ToolCall:
    """A single tool invocation the model requested."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class ProviderResponse:
    """One model turn, shaped provider-agnostic."""

    # Raw content blocks for replay into the next turn (Anthropic format).
    content_blocks: list[dict[str, Any]]
    # Convenience: concatenated text blocks for display.
    text: str
    # Tool calls the model made this turn.
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Typically "end_turn" or "tool_use".
    stop_reason: str = ""
    # Token usage, if the provider reported it.
    usage: dict[str, int] = field(default_factory=dict)


class Provider(Protocol):
    """Minimal LLM provider interface.

    One method: take a system prompt + conversation + tool schema, return
    one turn. The tool loop handles the "call-tools-and-loop" dance on
    top of this.
    """

    async def message(
        self,
        *,
        system: str,
        messages: list[ProviderMessage],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int = 4096,
    ) -> ProviderResponse: ...
