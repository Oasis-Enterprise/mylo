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

    # Original turns unchanged; one new synthetic turn appended.
    assert len(repaired) == 3
    assert len(inserted) == 1
    blocks = inserted[0]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["t2"]


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


async def test_manager_load_persists_repairs(tmp_path: Path) -> None:
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

    # First load repairs and persists.
    m1 = ConversationManager(storage=storage, conversation_id=conv_id)
    await m1.load()
    assert len(m1.history) == 3
    assert m1.history[2]["content"][0]["type"] == "tool_result"

    # Second load sees the already-persisted repair; no new repairs needed.
    m2 = ConversationManager(storage=storage, conversation_id=conv_id)
    await m2.load()
    assert len(m2.history) == 3
    # The repair only fires once per orphan, so DB should have exactly 3 rows.
    rows = await storage.load(conv_id)
    assert len(rows) == 3
