"""SQLite-backed conversation log.

Single continuous thread per user (spec decision: no multi-thread UI in v1).
One table, two content columns:

* ``role`` — ``"user"`` or ``"assistant"``
* ``content_json`` — the full content blocks (list or str) serialized as JSON

This is the source of truth for conversation history; the in-memory
:class:`mylo.conversation.manager.ConversationManager` hydrates from it on
startup and appends on each turn.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    user_id         TEXT,
    role            TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    prompt_version  TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
"""


class ConversationStorage:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def append(
        self,
        *,
        conversation_id: str,
        role: str,
        content: Any,
        user_id: str | None = None,
        prompt_version: str | None = None,
    ) -> int:
        payload = json.dumps(content, default=str)
        created_at = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                INSERT INTO messages (conversation_id, user_id, role,
                    content_json, prompt_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, user_id, role, payload, prompt_version, created_at),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def load(self, conversation_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return messages in chronological order, oldest first."""
        query = (
            "SELECT id, role, content_json, created_at, prompt_version "
            "FROM messages WHERE conversation_id = ? ORDER BY id ASC"
        )
        args: tuple[Any, ...] = (conversation_id,)
        if limit is not None:
            # When a limit is set, take the most recent N then reverse.
            query = (
                "SELECT id, role, content_json, created_at, prompt_version "
                "FROM (SELECT * FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT ?) AS recent ORDER BY id ASC"
            )
            args = (conversation_id, limit)

        out: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._path) as db, db.execute(query, args) as cursor:
            async for row in cursor:
                out.append(
                    {
                        "id": row[0],
                        "role": row[1],
                        "content": json.loads(row[2]),
                        "created_at": row[3],
                        "prompt_version": row[4],
                    }
                )
        return out

    async def clear(self, conversation_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            await db.commit()
