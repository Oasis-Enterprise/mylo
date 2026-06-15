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

"""Post-turn tool-result compression for conversation history.

After the model processes a tool result and produces its response,
the full result payload is no longer needed verbatim — the model
already reasoned over it. Replacing it with a compact summary saves
thousands of tokens per subsequent turn.

A single ``query_entities`` returning 200 entities is ~8,000 tokens.
After summarization it's ~200. That's 7,800 tokens saved on every
turn for the rest of the conversation.

The summarizer runs after each complete turn (both halves: the
user-message-with-tool-results and the assistant-text-response
have landed in history). It walks backward through the history and
compresses tool_result content blocks that are older than the
current turn.
"""

from __future__ import annotations

import json
from typing import Any

from mylo.logging_setup import get_logger

log = get_logger(__name__)

# Tool results below this size (in chars) aren't worth compressing —
# the overhead of the summary itself would be comparable.
_MIN_COMPRESS_CHARS = 500

# Tools whose results should never be compressed (they're consumed
# by the next turn's approval flow).
_NEVER_COMPRESS = frozenset({"call_service", "reload_config"})


def compress_old_tool_results(
    history: list[dict[str, Any]],
    *,
    keep_last_n_turns: int = 2,
) -> list[dict[str, Any]]:
    """Return a copy of ``history`` with old tool_result content
    blocks replaced by compact summaries.

    ``keep_last_n_turns`` controls how many recent assistant turns
    retain their full tool results. The most recent turns need full
    fidelity for follow-up questions; older turns are compressed.
    """
    # Find indices of assistant turns (descending) so we know which
    # tool results are "recent" vs "old".
    assistant_indices: list[int] = []
    for i, msg in enumerate(history):
        if msg.get("role") == "assistant":
            assistant_indices.append(i)

    # The last N assistant turns and everything after them keep full
    # results. Everything before is fair game for compression.
    if len(assistant_indices) <= keep_last_n_turns:
        return history  # nothing old enough to compress

    cutoff_index = assistant_indices[-keep_last_n_turns]

    compressed = []
    for i, msg in enumerate(history):
        if i >= cutoff_index:
            compressed.append(msg)
            continue

        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            compressed.append(msg)
            continue

        # Walk content blocks looking for tool_result to compress.
        new_blocks = []
        changed = False
        for block in msg["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                new_blocks.append(block)
                continue

            raw_content = block.get("content", "")
            if len(raw_content) < _MIN_COMPRESS_CHARS:
                new_blocks.append(block)
                continue

            summary = _summarize_tool_result(raw_content)
            if summary is None:
                new_blocks.append(block)
                continue

            new_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": summary,
                }
            )
            changed = True

        if changed:
            compressed.append({"role": "user", "content": new_blocks})
        else:
            compressed.append(msg)

    return compressed


def _summarize_tool_result(raw_content: str) -> str | None:
    """Parse the JSON tool result and produce a compact summary.

    Returns None if we can't parse or don't know how to summarize
    (caller keeps the original).
    """
    try:
        envelope = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(envelope, dict):
        return None

    status = envelope.get("status", "ok")
    data = envelope.get("data")

    if status != "ok" or not isinstance(data, dict):
        # Errors are already short; keep them.
        return None

    parts: list[str] = [f"status: {status}"]

    # query_entities — keep the entity_ids, drop the bulky state/attributes.
    # The ids are what the model needs to build dashboards/automations;
    # dropping them (as we used to) made the model re-query the same
    # entities over and over. The ids are far smaller than the full rows.
    if "entities" in data and isinstance(data["entities"], list):
        entities = data["entities"]
        count = len(entities)
        total = data.get("total_before_limit", count)
        summary_line = data.get("summary")
        parts.append(f"returned {count} of {total} entities")
        if summary_line:
            parts.append(f"summary: {summary_line}")
        if data.get("truncated"):
            parts.append("(truncated)")
        ids = [e["entity_id"] for e in entities if isinstance(e, dict) and e.get("entity_id")]
        return json.dumps(
            {
                "status": status,
                "compressed": True,
                "summary": ". ".join(parts),
                "entity_ids": ids,
            }
        )

    # query_devices.
    if "devices" in data and isinstance(data["devices"], list):
        count = len(data["devices"])
        parts.append(f"returned {count} devices")
        return json.dumps({"status": status, "compressed": True, "summary": ". ".join(parts)})

    # query_automations.
    if "automations" in data and isinstance(data["automations"], list):
        count = len(data["automations"])
        parts.append(f"returned {count} automations")
        return json.dumps({"status": status, "compressed": True, "summary": ". ".join(parts)})

    # query_logs.
    if "entries" in data and isinstance(data["entries"], list):
        count = len(data["entries"])
        parts.append(f"returned {count} log entries")
        return json.dumps({"status": status, "compressed": True, "summary": ". ".join(parts)})

    # Dry-run previews — keep those since they might be referenced
    # later in the approval flow.
    if data.get("preview"):
        return None

    # Dashboard query results — the model uses the card list to build
    # surgical edits (replace_card, update_view). A compressed summary
    # would force the model to re-query and stall the approval flow.
    if (
        "view" in data
        or (
            isinstance(data.get("views"), list)
            and any(isinstance(v, dict) and "path" in v for v in data["views"])
        )
        or isinstance(data.get("dashboards"), list)
    ):
        return None

    # Generic fallback for large results: keep just the top-level keys.
    top_keys = list(data.keys())[:10]
    char_count = len(raw_content)
    parts.append(f"result had {char_count} chars, keys: {top_keys}")
    return json.dumps({"status": status, "compressed": True, "summary": ". ".join(parts)})


def _infer_tool_name(data: dict[str, Any]) -> str | None:
    """Best-effort tool name inference from the result shape."""
    if "entities" in data:
        return "query_entities"
    if "devices" in data:
        return "query_devices"
    if "automations" in data:
        return "query_automations"
    return None
