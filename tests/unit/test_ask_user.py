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

"""The ask_user tool and the tool-loop pause it triggers.

ask_user lets the model ask the user a structured question (theme,
layout, scope) with clickable options. The pause is enforced in
run_turn — after an ask_user result the loop breaks with
stop_reason='awaiting_user_input' — so the model can never answer its
own question. The user's choice arrives as the next chat message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mylo.conversation.manager import ConversationManager
from mylo.conversation.storage import ConversationStorage
from mylo.ha.registries import Registries
from mylo.llm.provider import ProviderResponse, ToolCall
from mylo.llm.tool_loop import DoneEvent, ToolResultEvent, run_turn
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _FakeProvider:
    def __init__(self, scripted: list[ProviderResponse]) -> None:
        self._queue = list(scripted)
        self.calls: list[dict[str, Any]] = []

    async def message(self, **kwargs: Any) -> ProviderResponse:
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError("fake provider ran out of scripted responses")
        return self._queue.pop(0)


_QUESTION_ARGS = {
    "question": "Which theme should the dashboard use?",
    "options": [
        {"label": "Noctis", "value": "noctis", "description": "Dark blue"},
        {"label": "Default", "value": "default"},
    ],
    "preference_key": "dashboard.theme",
}


def _tool_batch(*calls: tuple[str, str, dict[str, Any]]) -> ProviderResponse:
    return ProviderResponse(
        content_blocks=[
            {"type": "tool_use", "id": tid, "name": name, "input": args}
            for tid, name, args in calls
        ],
        text="",
        tool_calls=[ToolCall(id=tid, name=name, input=args) for tid, name, args in calls],
        stop_reason="tool_use",
        usage={"input_tokens": 20, "output_tokens": 10},
    )


@pytest.fixture(autouse=True)
def _load_tools():
    from mylo.tools import executor

    tool_registry._reset_for_tests()
    tool_registry.load_all()
    executor._result_cache.clear()
    yield
    tool_registry._reset_for_tests()
    executor._result_cache.clear()


@pytest.fixture
async def _conv(tmp_path: Path) -> ConversationManager:
    storage = ConversationStorage(tmp_path / "conv.db")
    await storage.init()
    return ConversationManager(storage=storage, conversation_id="test")


# ─── the tool itself ────────────────────────────────────────────────────────


async def test_ask_user_returns_await_flag(tmp_path):
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)
    result = await execute("ask_user", _QUESTION_ARGS, ctx)
    assert result.status.value == "ok", result
    assert result.data["await_user_input"] is True
    assert result.data["question"] == _QUESTION_ARGS["question"]
    assert result.data["options"][0]["label"] == "Noctis"
    assert result.data["preference_key"] == "dashboard.theme"


async def test_ask_user_rejects_too_many_options(tmp_path):
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)
    result = await execute(
        "ask_user",
        {
            "question": "pick one",
            "options": [{"label": f"opt{i}"} for i in range(9)],
        },
        ctx,
    )
    assert result.status.value != "ok"


async def test_ask_user_allows_no_options_with_free_text(tmp_path):
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)
    result = await execute("ask_user", {"question": "What vibe do you want?"}, ctx)
    assert result.status.value == "ok", result
    assert result.data["await_user_input"] is True


# ─── the loop pause ─────────────────────────────────────────────────────────


async def _run(provider, conv, tmp_path):
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path)
    return [
        e
        async for e in run_turn(
            user_message="build me a dashboard",
            conversation=conv,
            provider=provider,
            ctx=ctx,
            system="you are a test",
            tools=[],
            model="fake-model",
        )
    ]


async def test_loop_pauses_after_ask_user(tmp_path, _conv):
    # Only ONE scripted response: if the loop tried a second round after
    # ask_user, the fake provider would raise.
    provider = _FakeProvider([_tool_batch(("toolu_1", "ask_user", _QUESTION_ARGS))])

    events = await _run(provider, _conv, tmp_path)

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == "awaiting_user_input"
    assert len(provider.calls) == 1

    # The tool_result was persisted before the pause — the next turn's
    # history (and UI hydration) needs it.
    last = _conv.history[-1]
    assert last["role"] == "user"
    blocks = last["content"]
    assert any(
        b.get("type") == "tool_result" and "await_user_input" in b.get("content", "")
        for b in blocks
    )


async def test_sibling_tool_in_same_batch_still_executes(tmp_path, _conv):
    provider = _FakeProvider(
        [
            _tool_batch(
                ("toolu_1", "query_dashboard", {}),
                ("toolu_2", "ask_user", _QUESTION_ARGS),
            )
        ]
    )

    events = await _run(provider, _conv, tmp_path)

    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert [r.name for r in results] == ["query_dashboard", "ask_user"]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[0].stop_reason == "awaiting_user_input"


async def test_loop_continues_normally_without_ask_user(tmp_path, _conv):
    provider = _FakeProvider(
        [
            _tool_batch(("toolu_1", "query_dashboard", {})),
            ProviderResponse(
                content_blocks=[{"type": "text", "text": "done"}],
                text="done",
                tool_calls=[],
                stop_reason="end_turn",
                usage={"input_tokens": 10, "output_tokens": 5},
            ),
        ]
    )

    events = await _run(provider, _conv, tmp_path)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[0].stop_reason == "end_turn"
    assert len(provider.calls) == 2


async def test_ask_user_result_shape_survives_serialization(tmp_path, _conv):
    """The UI reads data.await_user_input from the SSE tool_result event —
    the envelope must carry it at data top level."""
    provider = _FakeProvider([_tool_batch(("toolu_1", "ask_user", _QUESTION_ARGS))])
    events = await _run(provider, _conv, tmp_path)
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.data["await_user_input"] is True
    assert json.dumps(result.data)  # JSON-serializable end to end
