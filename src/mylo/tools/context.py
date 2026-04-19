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

"""Execution context passed to every tool handler.

Keeps tools decoupled from global singletons. The executor is the only place
that constructs a :class:`ToolContext`, and does so from whatever long-lived
services the server/CLI has already wired up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mylo.config import AppConfig
from mylo.ha.registries import Registries
from mylo.ha.states import StatesCache
from mylo.ha.ws_client import HaWsClient
from mylo.safety.audit import AuditLogger
from mylo.safety.permissions import Permissions


@dataclass(slots=True)
class ToolContext:
    ws_client: HaWsClient
    registries: Registries
    config: AppConfig
    permissions: Permissions
    audit: AuditLogger
    states: StatesCache = field(default_factory=StatesCache)
    # Conversation id is relevant for audit logging, rate limits, and memory scope.
    conversation_id: str = "cli"
    # Set by the caller when the user has approved a tier-2/3 action (e.g.
    # after seeing a dry-run preview). Tier-1 tools ignore it.
    user_approved: bool = False
    # Whether this invocation is a dry-run. Tier-2 tools branch on this;
    # tier-1 tools ignore it.
    dry_run: bool = False
