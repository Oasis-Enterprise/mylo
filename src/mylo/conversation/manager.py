"""In-memory conversation manager backed by :class:`ConversationStorage`.

M4a scope: hydrate on start, append on each turn, flush immediately to
SQLite so a crash never loses a turn. No summarization yet — that's M4b
along with the full context assembler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mylo.conversation.storage import ConversationStorage
from mylo.llm.provider import ProviderMessage


@dataclass(slots=True)
class ConversationManager:
    storage: ConversationStorage
    conversation_id: str = "default"
    user_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    async def load(self, limit: int | None = None) -> None:
        self.history = await self.storage.load(self.conversation_id, limit=limit)

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
        """Cast the history into the provider-facing shape. Same structure
        today; keeps a seam for future filtering / truncation.
        """
        return [
            ProviderMessage(role=m["role"], content=m["content"]) for m in self.history
        ]

    async def clear(self) -> None:
        self.history = []
        await self.storage.clear(self.conversation_id)
