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

"""Tests for conversation repair-on-load.

Interrupted sessions leave orphaned ``tool_use`` blocks in storage with no
``tool_result`` follow-up. Anthropic rejects that on the next request.
:mod:`mylo.conversation.manager` repairs at load time and persists the
repair so future loads stay clean.
"""

from __future__ import annotations

import json
from pathlib import Path

from mylo.conversation.manager import (
    ConversationManager,
    _repair_orphaned_tool_uses,
    _trim_to_clean_start,
)
from mylo.conversation.storage import ConversationStorage


def test_repair_inserts_synthetic_tool_result_after_orphan_assistant() -> None:
    history = [
        {"role": "user", "content": "are my automations broken?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "t1", "name": "query_automations", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "query_logs", "input": {}},
            ],
        },
        # Interrupted — no tool_result turn follows.
    ]

    repaired, inserted = _repair_orphaned_tool_uses(history)

    assert len(repaired) == 3
    synth = repaired[2]
    assert synth["role"] == "user"
    blocks = synth["content"]
    assert {b["tool_use_id"] for b in blocks} == {"t1", "t2"}
    for block in blocks:
        assert block["type"] == "tool_result"
        payload = json.loads(block["content"])
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "interrupted"

    assert inserted == [synth]


def test_repair_is_noop_when_tool_uses_are_answered() -> None:
    history = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "echo", "input": {"v": 1}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": '{"status":"ok"}'}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]

    repaired, inserted = _repair_orphaned_tool_uses(history)

    assert inserted == []
    assert repaired == history


def test_repair_handles_partial_answer() -> None:
    # Two tool_use, only one answered.
    history = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "b", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
        },
    ]

    repaired, inserted = _repair_orphaned_tool_uses(history)

    # The synthetic result MERGES into the existing partial results
    # message — a separate consecutive user message would leave the
    # original t1 result facing the synth message instead of its
    # tool_use, which Anthropic rejects.
    assert len(repaired) == 2
    assert len(inserted) == 1
    result_ids = {b["tool_use_id"] for b in repaired[1]["content"]}
    assert result_ids == {"t1", "t2"}


def test_trim_drops_leading_orphan_tool_result() -> None:
    # Limit=2 cut mid-pair: history starts with a user tool_result whose
    # assistant tool_use got dropped from the window.
    history = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]

    trimmed = _trim_to_clean_start(history)

    # Both leading messages dropped; the assistant turn can't lead a window
    # either (must start with user).
    assert trimmed == []


def test_trim_drops_leading_assistant_but_keeps_user_after() -> None:
    history = [
        {"role": "assistant", "content": [{"type": "text", "text": "stray"}]},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]

    trimmed = _trim_to_clean_start(history)
    assert [m["role"] for m in trimmed] == ["user", "assistant"]


def test_trim_keeps_plain_user_start() -> None:
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    assert _trim_to_clean_start(history) == history


async def test_manager_load_repairs_in_memory_only(tmp_path: Path) -> None:
    storage = ConversationStorage(tmp_path / "conv.db")
    await storage.init()

    # Simulate a crashed session: assistant tool_use, no tool_result.
    conv_id = "stuck"
    await storage.append(conversation_id=conv_id, user_id=None, role="user", content="hi")
    await storage.append(
        conversation_id=conv_id,
        user_id=None,
        role="assistant",
        content=[{"type": "tool_use", "id": "t1", "name": "echo", "input": {}}],
    )

    # Load repairs in memory — but storage stays exactly the 2 rows we
    # appended. Persisting repairs used to cause synthetic tool_results
    # to drift to the end of storage, breaking Anthropic's "immediately
    # after" adjacency rule in later windows.
    m1 = ConversationManager(storage=storage, conversation_id=conv_id)
    await m1.load()
    assert len(m1.history) == 3
    assert m1.history[2]["content"][0]["type"] == "tool_result"

    rows = await storage.load(conv_id)
    assert len(rows) == 2  # no repair row persisted

    # Second load regenerates the in-memory repair; still 2 rows in DB.
    m2 = ConversationManager(storage=storage, conversation_id=conv_id)
    await m2.load()
    assert len(m2.history) == 3
    rows = await storage.load(conv_id)
    assert len(rows) == 2


async def test_as_provider_messages_strips_leading_orphan(tmp_path: Path) -> None:
    # Simulate a poisoned in-memory history that somehow still has an
    # orphan leading tool_result. The provider boundary must clean it.
    storage = ConversationStorage(tmp_path / "conv.db")
    await storage.init()
    m = ConversationManager(storage=storage, conversation_id="x")
    m.history = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "ghost", "content": "{}"}],
        },
        {"role": "user", "content": "hi"},
    ]

    msgs = m.as_provider_messages()
    assert [x["role"] for x in msgs] == ["user"]
    assert msgs[0]["content"] == "hi"


# ─── peek(): read-only view for the UI polling endpoint ─────────────────────
#
# GET /api/conversation used to call conv.load(limit=12), which REASSIGNED
# conv.history mid-turn. If the 12-row window held only assistant/tool_result
# rows, _trim_to_clean_start returned [] and the in-flight run_turn lost its
# entire history — burning 25 iterations of empty_messages_fallback. peek()
# reads storage without touching the live history.


async def test_peek_does_not_mutate_live_history(tmp_path: Path) -> None:
    storage = ConversationStorage(tmp_path / "conv.db")
    await storage.init()
    conv = ConversationManager(storage=storage, conversation_id="c1")

    await conv.append("user", "build me a dashboard")
    await conv.append(
        "assistant",
        [{"type": "tool_use", "id": "t1", "name": "query_entities", "input": {}}],
    )
    await conv.append(
        "user",
        [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
    )
    live_history = conv.history

    rows = await conv.peek(limit=12)

    assert conv.history is live_history
    assert len(conv.history) == 3
    assert len(rows) == 3
    assert rows[0]["role"] == "user"


async def test_peek_with_dirty_window_leaves_history_alone(tmp_path: Path) -> None:
    """A storage window holding only mid-turn rows (no clean user start)
    must not empty the live history — the old load() path did exactly that."""
    storage = ConversationStorage(tmp_path / "conv.db")
    await storage.init()
    conv = ConversationManager(storage=storage, conversation_id="c1")

    await conv.append("user", "hi")
    for i in range(8):
        await conv.append(
            "assistant",
            [{"type": "tool_use", "id": f"t{i}", "name": "echo", "input": {}}],
        )
        await conv.append(
            "user",
            [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "{}"}],
        )

    # limit=4 lands entirely inside the tool cycle — no clean start.
    rows = await conv.peek(limit=4)

    assert len(rows) == 4  # raw rows, verbatim
    assert len(conv.history) == 17  # untouched
    assert conv.as_provider_messages()  # the live turn still has context


# ─── Orphaned tool_result repair (concurrent-turn interleaving) ─────────────
#
# Two turns running concurrently on the singleton conversation interleave
# their appends: turn A's assistant(tool_use P) can be followed by turn
# B's messages before turn A appends user(result P). Anthropic then 400s:
# "unexpected tool_use_id found in tool_result blocks — each tool_result
# must have a corresponding tool_use in the previous message". The repair
# must DROP result blocks that no longer face their tool_use, and the
# synth-result repair must MERGE into a partial results message instead
# of inserting a second consecutive user message (same 400 otherwise).


def _valid_for_anthropic(messages: list[dict]) -> bool:
    """Mirror the API's tool_result placement rule."""
    for i, msg in enumerate(messages):
        if msg["role"] != "user" or not isinstance(msg.get("content"), list):
            continue
        prev = messages[i - 1] if i > 0 else None
        allowed: set[str] = set()
        if prev and prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
            allowed = {
                b["id"]
                for b in prev["content"]
                if isinstance(b, dict) and b.get("type") == "tool_use"
            }
        for block in msg["content"]:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") not in allowed
            ):
                return False
    return True


def test_interleaved_turns_produce_valid_provider_messages(tmp_path: Path) -> None:
    """The exact production corruption: turn B's messages landed between
    turn A's assistant(tool_use) and its user(tool_result)."""
    storage = ConversationStorage(tmp_path / "conv.db")
    conv = ConversationManager(storage=storage, conversation_id="c1")
    conv.history = [
        {"role": "user", "content": "fix my automations"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tA", "name": "patch", "input": {}}],
        },
        # Turn B interleaves before turn A's result lands:
        {"role": "user", "content": "hello again"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tB", "name": "query", "input": {}}],
        },
        # Turn A's result — its tool_use is no longer the previous message.
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tA", "content": "{}"}],
        },
        # Turn B's result — also orphaned now (previous is turn A's result msg).
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tB", "content": "{}"}],
        },
    ]

    messages = [{"role": m["role"], "content": m["content"]} for m in conv.as_provider_messages()]

    assert _valid_for_anthropic(messages)
    # Both dangling tool_uses got answered (synthetically or otherwise).
    for msg in messages:
        if msg["role"] == "assistant" and isinstance(msg["content"], list):
            uses = [b["id"] for b in msg["content"] if b.get("type") == "tool_use"]
            if not uses:
                continue
            nxt = messages[messages.index(msg) + 1]
            answered = {
                b.get("tool_use_id")
                for b in nxt["content"]
                if isinstance(nxt["content"], list) and isinstance(b, dict)
            }
            assert set(uses) <= answered


def test_partial_results_merge_instead_of_consecutive_user_messages(
    tmp_path: Path,
) -> None:
    """A partially-answered batch must not repair into two back-to-back
    user messages — the second one's results face the synth message, not
    the tool_use, and the API rejects it identically."""
    storage = ConversationStorage(tmp_path / "conv.db")
    conv = ConversationManager(storage=storage, conversation_id="c1")
    conv.history = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "b", "input": {}},
            ],
        },
        # Only t1 answered — t2's result was lost mid-interruption.
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
        },
    ]

    messages = [{"role": m["role"], "content": m["content"]} for m in conv.as_provider_messages()]

    assert _valid_for_anthropic(messages)
    # One merged results message, not synth + original.
    assert len(messages) == 3
    result_ids = {b["tool_use_id"] for b in messages[2]["content"]}
    assert result_ids == {"t1", "t2"}


def test_orphaned_result_with_no_assistant_before_it_is_dropped(tmp_path: Path) -> None:
    storage = ConversationStorage(tmp_path / "conv.db")
    conv = ConversationManager(storage=storage, conversation_id="c1")
    conv.history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "sure"}]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "ghost", "content": "{}"}],
        },
        {"role": "user", "content": "and another thing"},
    ]

    messages = [{"role": m["role"], "content": m["content"]} for m in conv.as_provider_messages()]

    assert _valid_for_anthropic(messages)
    # The ghost-result message vanished entirely; real turns intact.
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
