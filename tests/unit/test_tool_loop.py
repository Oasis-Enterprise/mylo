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


async def test_empty_messages_falls_back_to_user_turn(
    tmp_path: Path, _conv: ConversationManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A broken/corrupted history can make trim+repair yield an empty list.
    # Anthropic 400s on an empty messages array ("at least one message is
    # required"); the loop must fall back to the user's current turn so the
    # request still goes through instead of crashing.
    monkeypatch.setattr(ConversationManager, "as_provider_messages", lambda self: [])
    provider = _FakeProvider([_text_turn("recovered")])
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    events = [
        e
        async for e in run_turn(
            user_message="please help",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="you are a test",
            tools=[],
            model="fake-model",
        )
    ]

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    # The provider was called with a non-empty messages list carrying the
    # user's turn — never an empty array.
    sent = provider.calls[0]["messages"]
    assert sent, "provider must never receive an empty messages list"
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"] == "please help"


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
    # The cap-hit must be surfaced, not silent: a pause message + flag.
    assert done[0].truncated is True
    text_events = [e for e in events if isinstance(e, TextEvent)]
    assert any("continue" in e.text.lower() for e in text_events)


async def test_natural_completion_is_not_truncated(
    tmp_path: Path, _conv: ConversationManager
) -> None:
    # One tool round, then the model ends the turn on its own.
    provider = _FakeProvider(
        [
            _tool_turn("toolu_1", "echo", {"value": 1}),
            _text_turn("all done", stop_reason="end_turn"),
        ]
    )
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)
    events = [
        e
        async for e in run_turn(
            user_message="do one thing",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="",
            tools=[],
            model="fake",
            max_iterations=5,
        )
    ]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[0].stop_reason == "end_turn"
    assert done[0].truncated is False


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


async def test_dashboard_cards_visible_after_intra_turn_compression(
    tmp_path: Path, _conv: ConversationManager
) -> None:
    """A multi-iteration turn must keep the dashboard view's card list
    reachable for later iterations. Without this, modify_dashboard can't
    construct a surgical replace_card / update_view patch and the model
    loops asking for the view config it was just shown.
    """

    # Re-register echo under a dashboard-shaped name so the executor
    # routes it via the same loop, but the result envelope mirrors what
    # query_dashboard actually returns (data.view.cards).
    class _DashParams(BaseModel):
        model_config = ConfigDict(extra="forbid")
        dashboard_id: str
        view_id: str

    big_cards = [
        {
            "type": "custom:mushroom-entity-card",
            "entity": f"switch.outside_{i}",
            "name": f"Outside {i}",
            "icon": "mdi:flower",
            "tap_action": {"action": "toggle"},
        }
        for i in range(8)
    ]

    async def _dashboard_handler(params: _DashParams, _ctx: Any) -> ToolResult:
        return ToolResult.ok(
            {
                "dashboard_id": params.dashboard_id,
                "view": {
                    "path": params.view_id,
                    "title": "Rooms",
                    "cards": big_cards,
                },
            }
        )

    tool_registry.register(
        ToolDefinition(
            name="query_dashboard",
            description="fake query_dashboard",
            params_model=_DashParams,
            tier=Tier.READ,
            handler=_dashboard_handler,
        )
    )

    provider = _FakeProvider(
        [
            _tool_turn("d1", "query_dashboard", {"dashboard_id": "lovelace", "view_id": "rooms"}),
            _tool_turn("e1", "echo", {"value": 1}),
            _tool_turn("e2", "echo", {"value": 2}),
            _text_turn("done."),
        ]
    )
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    [
        _e
        async for _e in run_turn(
            user_message="add a landscape chip",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="",
            tools=[],
            model="fake",
        )
    ]

    # Final provider call carries everything the model sees right before
    # the modify step would land. The dashboard cards from iteration 1
    # must still be reachable inside the tool_result payload.
    final_call = provider.calls[-1]
    serialized = json.dumps(final_call["messages"], default=str)
    assert "switch.outside_0" in serialized, (
        "dashboard cards were stripped by intra-turn compression — "
        "the model can't see the view it just queried"
    )
    assert "switch.outside_7" in serialized


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


async def test_identical_read_calls_deduped_and_nudged(
    tmp_path: Path, _conv: ConversationManager
) -> None:
    """Identical read calls within a turn are served from the per-turn cache
    (handler runs once) and a nudge is injected once repeats persist."""
    runs = {"n": 0}

    async def _counted(params: _Params, _ctx: Any) -> ToolResult:
        runs["n"] += 1
        return ToolResult.ok({"value": params.value})

    tool_registry.register(
        ToolDefinition(
            name="counted",
            description="counts executions",
            params_model=_Params,
            tier=Tier.READ,
            handler=_counted,
        )
    )

    # Same read 3 times, then the model ends the turn.
    provider = _FakeProvider(
        [
            _tool_turn("c1", "counted", {"value": 7}),
            _tool_turn("c2", "counted", {"value": 7}),
            _tool_turn("c3", "counted", {"value": 7}),
            _text_turn("done", stop_reason="end_turn"),
        ]
    )
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)

    _ = [
        e
        async for e in run_turn(
            user_message="go",
            conversation=_conv,
            provider=provider,
            ctx=ctx,
            system="",
            tools=[],
            model="fake",
            max_iterations=8,
        )
    ]

    # Executed exactly once; the 2nd and 3rd identical calls were served
    # from the per-turn dedup cache.
    assert runs["n"] == 1
    # The nudge text was injected into history after repeats crossed the
    # threshold.
    text_blocks = [
        b.get("text", "")
        for m in _conv.history
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert any("already fetched" in t.lower() for t in text_blocks)
