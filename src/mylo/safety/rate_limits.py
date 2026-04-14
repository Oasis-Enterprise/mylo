"""Rate limiting for tool calls.

Spec §5.5. Two layers:

* **Daily counters** — reset at UTC midnight. Bound total damage.
* **Per-conversation counters** — reset when a new ``conversation_id`` is
  seen. Bound surprise within one session.

Counters are held in memory. Process restarts reset them — acceptable for
v1; if persistence matters we'll push them to the SQLite conversation DB
in M4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date
from typing import Literal

Scope = Literal["service_calls", "file_writes", "entity_renames", "tier_3_calls"]

# Spec §5.5 defaults. Configurable per-deployment later.
DEFAULT_DAILY_LIMITS: dict[Scope, int] = {
    "file_writes": 20,
    "service_calls": 100,
    "entity_renames": 50,
    "tier_3_calls": 50,
}
# Per-conversation bound for any tier-3 tool burst.
DEFAULT_PER_CONVERSATION_LIMITS: dict[Scope, int] = {
    "tier_3_calls": 50,
}


class RateLimitExceeded(Exception):
    def __init__(self, scope: Scope, limit: int, kind: str) -> None:
        super().__init__(f"{kind} limit for {scope} ({limit}) exceeded")
        self.scope = scope
        self.limit = limit
        self.kind = kind


@dataclass(slots=True)
class RateLimitCounters:
    daily_limits: dict[Scope, int] = field(default_factory=lambda: dict(DEFAULT_DAILY_LIMITS))
    per_conversation_limits: dict[Scope, int] = field(
        default_factory=lambda: dict(DEFAULT_PER_CONVERSATION_LIMITS)
    )
    _daily_counts: dict[Scope, int] = field(default_factory=dict)
    _daily_as_of: date | None = None
    _conversation_counts: dict[str, dict[Scope, int]] = field(default_factory=dict)

    def _roll_day(self) -> None:
        today = date.today()
        # Actually use UTC for reproducibility.
        today_utc = _today_utc()
        if self._daily_as_of != today_utc:
            self._daily_counts.clear()
            self._daily_as_of = today_utc
        _ = today  # kept for clarity; replaced by today_utc above

    def check_and_increment(
        self,
        scope: Scope,
        *,
        conversation_id: str,
    ) -> None:
        """Check both counters; on pass, increment both. On fail, raise
        without incrementing either.
        """
        self._roll_day()

        daily = self.daily_limits.get(scope)
        if daily is not None and self._daily_counts.get(scope, 0) >= daily:
            raise RateLimitExceeded(scope, daily, "daily")

        per_conv = self.per_conversation_limits.get(scope)
        if per_conv is not None:
            seen = self._conversation_counts.setdefault(conversation_id, {})
            if seen.get(scope, 0) >= per_conv:
                raise RateLimitExceeded(scope, per_conv, "per-conversation")

        self._daily_counts[scope] = self._daily_counts.get(scope, 0) + 1
        if per_conv is not None:
            seen = self._conversation_counts.setdefault(conversation_id, {})
            seen[scope] = seen.get(scope, 0) + 1


def _today_utc() -> date:
    from datetime import datetime

    return datetime.now(UTC).date()
