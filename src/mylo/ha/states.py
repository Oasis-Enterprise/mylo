"""Helpers for querying live entity state via the HA websocket API.

Unlike the registries (which describe *what exists*), state is live data that
changes every few seconds. We don't cache it — each call fetches fresh.
"""

from __future__ import annotations

from typing import Any

from mylo.ha.ws_client import HaWsClient


async def get_all_states(client: HaWsClient) -> dict[str, dict[str, Any]]:
    """Return a mapping ``entity_id → state dict`` for every entity in HA."""
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
