"""Tests for the LLM tool loop.

A FakeProvider is scripted with a list of responses; each provider.message
call consumes the next one. We verify:

* Text-only responses end the turn after one round.
* Tool calls are dispatched to the executor and their results are fed
  back as tool_result content blocks.
* Multi-round conversations (tool use → final text) work.
* Safety limit: ``max_iterations`` caps runaway loops.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from mylo.conversation.manager import ConversationManager
from mylo.conversation.storage import ConversationStorage
from mylo.ha.registries import Registries
from mylo.llm.provider import ProviderResponse, ToolCall
from mylo.llm.tool_loop import (
    DoneEvent,
    TextEvent,
    ToolCallEvent,
    ToolResultEvent,
    run_turn,
)
from mylo.tools import registry as tool_registry
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from tests.unit._helpers import make_ctx

# ─── Fake provider ───────────────────────────────────────────────────────────


class _FakeProvider:
    def __init__(self, scripted: list[ProviderResponse]) -> None:
        self._queue = list(scripted)
        self.calls: list[dict[str, Any]] = []

    async def message(self, **kwargs: Any) -> ProviderResponse:
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError("fake provider ran out of scripted responses")
        return self._queue.pop(0)


def _text_turn(text: str, *, stop_reason: str = "end_turn") -> ProviderResponse:
    return ProviderResponse(
        content_blocks=[{"type": "text", "text": text}],
        text=text,
        tool_calls=[],
        stop_reason=stop_reason,
        usage={"input_tokens": 10, "output_tokens": 5},
    )


def _tool_turn(tool_id: str, name: str, args: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        content_blocks=[{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
        text="",
        tool_calls=[ToolCall(id=tool_id, name=name, input=args)],
        stop_reason="tool_use",
        usage={"input_tokens": 20, "output_tokens": 10},
    )


# ─── A tool we can dispatch to ──────────────────────────────────────────────


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int = Field(ge=0)


async def _echo_handler(params: _Params, _ctx: Any) -> ToolResult:
    return ToolResult.ok({"echoed": params.value})


async def _boom_handler(_params: _Params, _ctx: Any) -> ToolResult:
    return ToolResult.error("boom", "handler said no", data={"hint": "try again"})


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    tool_registry._reset_for_tests()
    tool_registry.register(
        ToolDefinition(
            name="echo",
            description="echoes input",
            params_model=_Params,
            tier=Tier.READ,
            handler=_echo_handler,
        )
    )
    tool_registry.register(
        ToolDefinition(
            name="boom",
            description="always fails",
            params_model=_Params,
            tier=Tier.READ,
            handler=_boom_handler,
        )
    )
    yield
    tool_registry._reset_for_tests()


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
async def _conv(tmp_path: Path) -> ConversationManager:
    storage = ConversationStorage(tmp_path / "conv.db")
    await storage.init()
    return ConversationManager(storage=storage, conversation_id="test")


# ─── Tests ──────────────────────────────────────────────────────────────────


async def test_text_only_response_ends_turn(tmp_path: Path, _conv: ConversationManager) -> None:
    provider = _FakeProvider([_text_turn("Hello there.")])
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    events = [
        e
        async for e in run_turn(
            user_message="hi",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="you are a test",
            tools=[],
            model="fake-model",
        )
    ]

    text_events = [e for e in events if isinstance(e, TextEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(text_events) == 1
    assert text_events[0].text == "Hello there."
    assert len(done) == 1
    assert done[0].stop_reason == "end_turn"
    # Conversation persisted: user + assistant.
    assert len(_conv.history) == 2
    assert _conv.history[0]["role"] == "user"
    assert _conv.history[0]["content"] == "hi"


async def test_tool_call_then_final_text(tmp_path: Path, _conv: ConversationManager) -> None:
    provider = _FakeProvider(
        [
            _tool_turn("toolu_1", "echo", {"value": 7}),
            _text_turn("Got 7 back."),
        ]
    )
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    events = [
        e
        async for e in run_turn(
            user_message="echo 7",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="you are a test",
            tools=[],
            model="fake",
        )
    ]

    call_events = [e for e in events if isinstance(e, ToolCallEvent)]
    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    text_events = [e for e in events if isinstance(e, TextEvent)]
    assert [c.name for c in call_events] == ["echo"]
    assert call_events[0].input == {"value": 7}
    assert result_events[0].status == "ok"
    assert result_events[0].data == {"echoed": 7}
    assert text_events[-1].text == "Got 7 back."

    # History: user, assistant(tool_use), user(tool_result), assistant(text).
    assert len(_conv.history) == 4
    assert _conv.history[2]["role"] == "user"
    result_block = _conv.history[2]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "toolu_1"
    inner = json.loads(result_block["content"])
    assert inner["status"] == "ok"


async def test_tool_error_still_feeds_result_and_loop_continues(
    tmp_path: Path, _conv: ConversationManager
) -> None:
    provider = _FakeProvider(
        [
            _tool_turn("toolu_x", "boom", {"value": 1}),
            _text_turn("Ah, it failed."),
        ]
    )
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    events = [
        e
        async for e in run_turn(
            user_message="please boom",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="",
            tools=[],
            model="fake",
        )
    ]
    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert result_events[0].status == "error"
    assert result_events[0].error_code == "boom"
    text_events = [e for e in events if isinstance(e, TextEvent)]
    assert text_events[-1].text == "Ah, it failed."


async def test_max_iterations_caps_runaway_loops(
    tmp_path: Path, _conv: ConversationManager
) -> None:
    # Model keeps asking for the tool, never ends turn.
    provider = _FakeProvider([_tool_turn(f"toolu_{i}", "echo", {"value": i}) for i in range(10)])
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    events = [
        e
        async for e in run_turn(
            user_message="go forever",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="",
            tools=[],
            model="fake",
            max_iterations=3,
        )
    ]
    call_events = [e for e in events if isinstance(e, ToolCallEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(call_events) == 3  # exactly max_iterations tool rounds
    assert done[0].stop_reason == "tool_use"  # model never ended turn


async def test_conversation_hydrates_from_storage(tmp_path: Path) -> None:
    storage = ConversationStorage(tmp_path / "conv.db")
    await storage.init()
    conv1 = ConversationManager(storage=storage, conversation_id="persist")
    await conv1.append("user", "earlier question")
    await conv1.append("assistant", [{"type": "text", "text": "earlier answer"}])

    conv2 = ConversationManager(storage=storage, conversation_id="persist")
    await conv2.load()
    assert [m["role"] for m in conv2.history] == ["user", "assistant"]
    assert conv2.history[0]["content"] == "earlier question"


async def test_multiple_text_blocks_yield_separate_events(
    tmp_path: Path, _conv: ConversationManager
) -> None:
    # Simulate a turn where the model interleaves text → tool → text.
    response = ProviderResponse(
        content_blocks=[
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {"value": 1}},
        ],
        text="Let me check.",
        tool_calls=[ToolCall(id="t1", name="echo", input={"value": 1})],
        stop_reason="tool_use",
        usage={},
    )
    final = ProviderResponse(
        content_blocks=[
            {"type": "text", "text": "First block."},
            {"type": "text", "text": "Second block."},
        ],
        text="First block.\nSecond block.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={},
    )
    provider = _FakeProvider([response, final])
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    events = [
        e
        async for e in run_turn(
            user_message="hi",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="",
            tools=[],
            model="fake",
        )
    ]

    text_events = [e for e in events if isinstance(e, TextEvent)]
    texts = [e.text for e in text_events]
    # Three text blocks expected: one from first turn, two from final.
    assert texts == ["Let me check.", "First block.", "Second block."]


async def test_usage_is_summed_across_iterations(
    tmp_path: Path, _conv: ConversationManager
) -> None:
    provider = _FakeProvider(
        [
            _tool_turn("t1", "echo", {"value": 1}),
            _text_turn("done"),
        ]
    )
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    events = [
        e
        async for e in run_turn(
            user_message="q",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="",
            tools=[],
            model="fake",
        )
    ]
    done = next(e for e in events if isinstance(e, DoneEvent))
    # 20+10 from tool turn plus 10+5 from text turn.
    assert done.usage["input_tokens"] == 30
    assert done.usage["output_tokens"] == 15
