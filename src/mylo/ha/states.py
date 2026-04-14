"""Helpers for querying live entity state via the HA websocket API.

``get_all_states`` is the one heavy call: on a 2000+ entity home the payload
is several megabytes over the websocket. Multiple tools firing in the same
turn (common when the model fans out on one question) would otherwise pay
that cost three or four times.

:class:`StatesCache` addresses this with a short TTL. Within a turn, all
tools share a single fetch; between turns the cache refreshes itself so the
model still sees live state. The lock serializes refreshes so concurrent
tool calls don't stampede the same backend.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mylo.ha.ws_client import HaWsClient


async def get_all_states(client: HaWsClient) -> dict[str, dict[str, Any]]:
    """Return a mapping ``entity_id → state dict`` for every entity in HA.

    Direct fetch — no caching. Use :class:`StatesCache` for the reuse-within-
    a-turn pattern.
    """
    raw = await client.send_command("get_states")
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if isinstance(entity_id, str):
            out[entity_id] = item
    return out


class StatesCache:
    """Short-TTL cache of entity states.

    One instance per :class:`mylo.tools.context.ToolContext`; shared by every
    tool in that context. TTL of 30 seconds by default — long enough to
    cover a multi-tool LLM turn, short enough that stale state doesn't
    linger into the next turn. Calling :meth:`invalidate` is a hard reset.
    """

    def __init__(self, ttl: float = 30.0) -> None:
        self._ttl = ttl
        self._data: dict[str, dict[str, Any]] | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, client: HaWsClient) -> dict[str, dict[str, Any]]:
        async with self._lock:
            now = time.monotonic()
            if self._data is None or (now - self._fetched_at) > self._ttl:
                self._data = await get_all_states(client)
                self._fetched_at = time.monotonic()
            return self._data

    def invalidate(self) -> None:
        self._data = None
        self._fetched_at = 0.0
