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

"""Tests for post-turn tool-result compression.

The compressor must not destroy data that the next turn needs to act on.
Dashboard edits in particular require knowing the existing card list so
the model can construct surgical replace_card / update_view patches —
without it the model loops asking for the view config it was just shown.
"""

from __future__ import annotations

import json
from typing import Any

from mylo.conversation.summarize import compress_old_tool_results


def _tool_result_msg(tool_use_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """Build a user-role message with one tool_result block."""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps(envelope),
            }
        ],
    }


def _assistant_text(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _big_card(idx: int) -> dict[str, Any]:
    # Realistic chip card with enough payload to push the envelope past
    # the 500-char compression threshold even on a small view.
    return {
        "type": "custom:mushroom-entity-card",
        "entity": f"switch.outside_{idx}",
        "name": f"Outside {idx}",
        "icon": "mdi:flower",
        "tap_action": {"action": "toggle"},
        "fill_container": True,
        "layout": "vertical",
    }


def test_dashboard_view_cards_survive_compression() -> None:
    """query_dashboard's `view` payload (with cards) must not be reduced
    to a "keys: [...]" stub. The follow-up turn needs the card list to
    build a replace_card / update_view patch.
    """
    view_envelope = {
        "status": "ok",
        "data": {
            "dashboard_id": "lovelace",
            "view": {
                "path": "rooms",
                "title": "Rooms",
                "icon": "mdi:home",
                "cards": [_big_card(i) for i in range(8)],
            },
        },
    }

    history = [
        {"role": "user", "content": "show me the rooms view"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "query_dashboard",
                    "input": {"dashboard_id": "lovelace", "view_id": "rooms"},
                }
            ],
        },
        _tool_result_msg("t1", view_envelope),
        _assistant_text("Here's the rooms view."),
        _assistant_text("Anything else?"),
    ]

    compressed = compress_old_tool_results(history, keep_last_n_turns=1)

    # Locate the (now-rewritten) tool_result block.
    tool_result_msgs = [
        m for m in compressed if m.get("role") == "user" and isinstance(m.get("content"), list)
    ]
    assert tool_result_msgs, "tool_result message went missing"
    block = tool_result_msgs[0]["content"][0]
    payload = json.loads(block["content"])

    # The data needed to construct a surgical patch — the cards list —
    # must still be reachable from the compressed envelope.
    data = payload.get("data") or {}
    view = data.get("view") or {}
    cards = view.get("cards")
    assert isinstance(cards, list), (
        f"dashboard view cards were stripped by compression: {payload!r}"
    )
    assert len(cards) == 8
    assert cards[0]["entity"] == "switch.outside_0"


def test_query_dashboard_summary_list_survives_compression() -> None:
    """The dashboard-summary shape (no view_id) returns `views: [{path,
    title, card_count}, ...]`. The model needs the view paths to pick
    the right one — generic-compressing to "keys: [...]" loses them.
    """
    summary_envelope = {
        "status": "ok",
        "data": {
            "dashboard_id": "lovelace",
            "title": "Overview",
            "view_count": 6,
            "views": [
                {"path": f"view_{i}", "title": f"View {i}", "card_count": 10} for i in range(6)
            ],
        },
    }
    # Pad so we cross the 500-char threshold.
    summary_envelope["data"]["title"] = "Overview " + ("x" * 600)

    history = [
        {"role": "user", "content": "what views do I have?"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "query_dashboard",
                    "input": {"dashboard_id": "lovelace"},
                }
            ],
        },
        _tool_result_msg("t2", summary_envelope),
        _assistant_text("You have six views."),
        _assistant_text("Want to edit one?"),
    ]

    compressed = compress_old_tool_results(history, keep_last_n_turns=1)
    block = next(
        m["content"][0]
        for m in compressed
        if m.get("role") == "user" and isinstance(m.get("content"), list)
    )
    payload = json.loads(block["content"])
    views = (payload.get("data") or {}).get("views")
    assert isinstance(views, list), f"view list stripped: {payload!r}"
    assert len(views) == 6
    assert views[0]["path"] == "view_0"
