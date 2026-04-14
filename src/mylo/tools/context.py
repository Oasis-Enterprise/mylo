"""Execution context passed to every tool handler.

Keeps tools decoupled from global singletons. The executor is the only place
that constructs a :class:`ToolContext`, and does so from whatever long-lived
services the server/CLI has already wired up.
"""

from __future__ import annotations

from dataclasses import dataclass

from mylo.config import AppConfig
from mylo.ha.registries import Registries
from mylo.ha.ws_client import HaWsClient


@dataclass(slots=True)
class ToolContext:
    ws_client: HaWsClient
    registries: Registries
    config: AppConfig
    # Conversation id is relevant for audit logging (M3) and memory scope (M8).
    conversation_id: str = "cli"
