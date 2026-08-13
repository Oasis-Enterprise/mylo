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

"""In-memory conversation manager backed by :class:`ConversationStorage`.

M4a scope: hydrate on start, append on each turn, flush immediately to
SQLite so a crash never loses a turn. No summarization yet — that's M4b
along with the full context assembler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mylo.conversation.storage import ConversationStorage
from mylo.llm.provider import ProviderMessage
from mylo.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ConversationManager:
    storage: ConversationStorage
    conversation_id: str = "default"
    user_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    # True while a run_turn is consuming this conversation. Handlers that
    # would replace history out from under the turn check this instead of
    # racing (single process, single event loop — a flag is sufficient).
    turn_active: bool = False

    async def peek(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Read-only view of stored rows for the UI.

        Never touches ``self.history`` — the UI polls this mid-turn (after
        an SSE drop), and reassigning history here used to wipe the
        in-flight turn's context: a limited window of only assistant /
        tool_result rows trims to [], and the running loop can never
        recover a clean start once its leading user turn is gone.
        """
        return await self.storage.load(self.conversation_id, limit=limit)

    async def load(self, limit: int | None = None) -> None:
        raw = await self.storage.load(self.conversation_id, limit=limit)
        # Truncating to a limit can cut mid tool_use/tool_result pair.
        # Drop leading fragments so the window always starts on a clean
        # user turn.
        trimmed = _trim_to_clean_start(raw)
        if len(trimmed) != len(raw):
            log.info(
                "conversation.trimmed_leading_fragments",
                conversation_id=self.conversation_id,
                dropped=len(raw) - len(trimmed),
            )
        trimmed, dropped_results = _drop_orphaned_tool_results(trimmed)
        if dropped_results:
            log.info(
                "conversation.dropped_orphaned_tool_results",
                conversation_id=self.conversation_id,
                tool_use_ids=dropped_results,
            )
        # Repairs are IN-MEMORY ONLY now. Persisting them used to append
        # synthetic tool_results to the END of storage (SQLite
        # autoincrement doesn't allow insert-at-position-K), which
        # broke Anthropic's "tool_result must be immediately after its
        # tool_use" rule in later windows. Regenerating on each load is
        # microseconds of work and strictly correct.
        self.history, repairs = _repair_orphaned_tool_uses(trimmed)
        for repair in repairs:
            log.info(
                "conversation.repaired_orphan_tool_use_in_memory",
                conversation_id=self.conversation_id,
                tool_use_ids=[
                    b["tool_use_id"]
                    for b in repair["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ],
            )

    async def append(self, role: str, content: Any, *, prompt_version: str | None = None) -> None:
        self.history.append({"role": role, "content": content})
        await self.storage.append(
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            role=role,
            content=content,
            prompt_version=prompt_version,
        )

    def as_provider_messages(self) -> list[ProviderMessage]:
        """Cast the history into the provider-facing shape.

        Re-runs the trim + repair at send time so any orphan created by
        a mid-session append (e.g. an in-flight tool_use that never
        completed) never reaches Anthropic. Cheap — the trim/repair are
        O(history length) and the history is trimmed to ~12 entries.
        """
        trimmed = _trim_to_clean_start(self.history)
        trimmed, dropped = _drop_orphaned_tool_results(trimmed)
        if dropped:
            log.warning(
                "conversation.dropped_orphaned_tool_results",
                conversation_id=self.conversation_id,
                tool_use_ids=dropped,
            )
        repaired, _ = _repair_orphaned_tool_uses(trimmed)
        return [ProviderMessage(role=m["role"], content=m["content"]) for m in repaired]

    async def clear(self) -> None:
        self.history = []
        await self.storage.clear(self.conversation_id)


def _trim_to_clean_start(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Skip leading messages until we find a clean conversation start.

    Anthropic requires messages[0] to be a user turn whose content is a
    string (or at least doesn't contain ``tool_result`` blocks — those
    need a preceding assistant ``tool_use``). When history is limited, we
    can land inside a tool cycle; drop leading messages until we're on a
    valid boundary.
    """
    i = 0
    while i < len(history):
        msg = history[i]
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            # Plain text user message is always a valid start.
            if isinstance(content, str):
                return history[i:]
            # A user message whose content is a list must not contain
            # tool_result blocks at the conversation boundary.
            if isinstance(content, list) and not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                return history[i:]
            i += 1
            continue

        # Leading assistant messages aren't valid either (must start with
        # user). Skip.
        i += 1

    return []


def _drop_orphaned_tool_results(
    history: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Remove ``tool_result`` blocks whose ``tool_use`` is not in the
    immediately preceding message.

    Anthropic rejects the whole request otherwise ("each tool_result
    block must have a corresponding tool_use block in the previous
    message"). This shape appears when two turns interleave their
    appends on the shared conversation — turn B's messages land between
    turn A's assistant(tool_use) and its user(tool_result). The results
    are unrecoverable in place (their tool_use is stranded messages
    back); dropping them lets :func:`_repair_orphaned_tool_uses`
    synthesize ``interrupted`` results for the stranded tool_uses, which
    restores a valid sequence.

    Returns ``(cleaned_history, dropped_tool_use_ids)``. Messages left
    with no content are removed entirely.
    """
    cleaned: list[dict[str, Any]] = []
    dropped: list[str] = []

    for msg in history:
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            cleaned.append(msg)
            continue

        prev = cleaned[-1] if cleaned else None
        allowed: set[str] = set()
        if (
            prev is not None
            and prev.get("role") == "assistant"
            and isinstance(prev.get("content"), list)
        ):
            allowed = {
                b["id"]
                for b in prev["content"]
                if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b
            }

        kept_blocks = []
        for block in msg["content"]:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") not in allowed
            ):
                dropped.append(str(block.get("tool_use_id")))
                continue
            kept_blocks.append(block)

        if not kept_blocks:
            continue
        if len(kept_blocks) != len(msg["content"]):
            cleaned.append({**msg, "content": kept_blocks})
        else:
            cleaned.append(msg)

    return cleaned, dropped


def _repair_orphaned_tool_uses(
    history: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """If an assistant turn declares tool_use blocks that aren't answered by
    a ``tool_result`` in the next message, synthesize a user message with
    ``status: error, code: interrupted`` tool_result blocks. Anthropic
    rejects dangling tool_use otherwise.

    Returns ``(repaired_history, inserted_turns)``. ``inserted_turns`` is
    what the caller should persist back to storage so the repair sticks.
    """
    repaired: list[dict[str, Any]] = []
    inserted: list[dict[str, Any]] = []

    # When the next message already carries SOME results (a partially
    # answered batch), the synth blocks must merge INTO it — inserting a
    # separate synth message would leave the original results facing the
    # synth message instead of their tool_use, which the API rejects the
    # same way as a missing result.
    merge_into_next: list[dict[str, Any]] = []

    i = 0
    while i < len(history):
        msg = history[i]
        if merge_into_next:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                msg = {**msg, "content": merge_into_next + list(msg["content"])}
            merge_into_next = []
        repaired.append(msg)

        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            tool_uses = [
                b for b in msg["content"] if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            if tool_uses:
                answered: set[str] = set()
                next_msg = history[i + 1] if i + 1 < len(history) else None
                next_is_result_msg = (
                    next_msg is not None
                    and next_msg.get("role") == "user"
                    and isinstance(next_msg.get("content"), list)
                )
                if next_is_result_msg and next_msg is not None:
                    for block in next_msg["content"]:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and isinstance(block.get("tool_use_id"), str)
                        ):
                            answered.add(block["tool_use_id"])

                unanswered = [tu for tu in tool_uses if tu.get("id") not in answered]
                if unanswered:
                    synth_blocks = [
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": json.dumps(
                                {
                                    "status": "error",
                                    "error": {
                                        "code": "interrupted",
                                        "message": (
                                            "previous session was interrupted "
                                            "before this tool completed"
                                        ),
                                    },
                                }
                            ),
                        }
                        for tu in unanswered
                    ]
                    if next_is_result_msg:
                        merge_into_next = synth_blocks
                        inserted.append({"role": "user", "content": synth_blocks})
                    else:
                        synth_msg = {"role": "user", "content": synth_blocks}
                        repaired.append(synth_msg)
                        inserted.append(synth_msg)

        i += 1

    return repaired, inserted
