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

"""Permission gate — the pre-execution check that runs on every tool call.

Spec §4.2 and §5.5. Responsibilities (for M3):

* Tier gate — tier-1 executes freely; tier-2 requires a prior dry_run
  approval or an explicit user_approved=True signal; tier-3 requires
  explicit approval *and* consumes a rate limit.
* Daily rate limits for tier-3 calls.

This module deliberately does NOT do:

* Dry-run enforcement (that pattern is enforced by individual write tools
  accepting a ``dry_run`` parameter and the executor tracking approvals —
  lands with M7).
* Chain checkpoint — comes with the conversation manager.
* Blocked/restricted service allow/block — lives in the tier-3 tools.

Return is a :class:`PermissionDecision` so the caller (executor) can
produce the right ToolResult envelope without us needing to know about
ToolResult here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mylo.safety.rate_limits import (
    RateLimitCounters,
    RateLimitExceeded,
)
from mylo.tools.base import Tier


@dataclass(slots=True)
class PermissionDecision:
    allowed: bool
    reason_code: Literal[
        "ok",
        "confirmation_required",
        "rate_limited",
        "blocked",
    ] = "ok"
    reason_message: str = ""


@dataclass(slots=True)
class Permissions:
    """Stateful permission gate. One instance per process, shared across
    conversations. Rate-limit counters live here.
    """

    rate_limits: RateLimitCounters

    def check(
        self,
        *,
        tier: Tier,
        conversation_id: str,
        user_approved: bool,
        dry_run: bool = False,
    ) -> PermissionDecision:
        if tier is Tier.READ:
            return PermissionDecision(allowed=True)

        # Tier-2 dry runs are previews — they validate, plan, and return a
        # diff, but don't write. Always allow without user_approved so the
        # model can propose → user sees → user approves.
        if tier is Tier.MODIFY and dry_run:
            return PermissionDecision(allowed=True)

        if not user_approved:
            return PermissionDecision(
                allowed=False,
                reason_code="confirmation_required",
                reason_message=(
                    f"tier-{tier.value} tools require user confirmation; "
                    "present a dry-run preview first and then retry with the "
                    "user's approval"
                ),
            )

        # Consume a rate-limit token for tier-3 here. Tier-2 writes are
        # rate-limited per scope by the individual tools (file_writes,
        # entity_renames) — they call into RateLimitCounters directly.
        if tier is Tier.ACTION:
            try:
                self.rate_limits.check_and_increment(
                    "tier_3_calls", conversation_id=conversation_id
                )
            except RateLimitExceeded as exc:
                return PermissionDecision(
                    allowed=False,
                    reason_code="rate_limited",
                    reason_message=str(exc),
                )

        return PermissionDecision(allowed=True)


def default_permissions() -> Permissions:
    return Permissions(rate_limits=RateLimitCounters())
