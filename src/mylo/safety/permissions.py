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
    ) -> PermissionDecision:
        if tier is Tier.READ:
            return PermissionDecision(allowed=True)

        if not user_approved:
            return PermissionDecision(
                allowed=False,
                reason_code="confirmation_required",
                reason_message=(
                    f"tier-{tier.value} tools require user confirmation; "
                    "present a dry-run preview and pass user_approved=True"
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
