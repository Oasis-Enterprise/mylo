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

"""Pydantic models for ``context.yaml`` — Mylo's persistent memory.

Matches the schema in spec §3.2. Every memory item carries metadata
(``created``, ``last_referenced``, ``reference_count``, ``priority``,
``ttl``) used by the pruner (§3.4) to decide what to drop first.

The root :class:`MemoryFile` is permissive about section content — the
sync job (M8b) will normalize shape drift. For reads during chat we
just need to load and render what exists without blowing up on
user-edited files.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NoteSource = Literal["conversation", "observation", "user_confirmed"]
Priority = Literal["critical", "normal", "low"]


class ItemMetadata(BaseModel):
    """Common metadata carried by every memory item."""

    model_config = ConfigDict(extra="allow")

    created: str | None = None
    last_referenced: str | None = None
    reference_count: int = 0
    source: NoteSource = "conversation"
    priority: Priority = "normal"
    ttl: str | None = None  # ISO date; null = indefinite


class Scope(BaseModel):
    model_config = ConfigDict(extra="allow")

    entity: str | None = None
    area: str | None = None
    general: bool | None = None


class HouseholdMember(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    role: str = "household_member"
    ha_user: str | None = None
    presence_entity: str | None = None
    notes: list[str] = Field(default_factory=list)


class Household(BaseModel):
    model_config = ConfigDict(extra="allow")

    members: list[HouseholdMember] = Field(default_factory=list)
    shared: dict[str, Any] = Field(default_factory=dict)


class DashboardPreferences(BaseModel):
    model_config = ConfigDict(extra="allow")

    card_style: str | None = None
    layout_preference: str | None = None
    theme: str | None = None
    notes: str | None = None


class NamingPreferences(BaseModel):
    model_config = ConfigDict(extra="allow")

    convention: str | None = None
    examples: list[dict[str, Any]] = Field(default_factory=list)


class AlertPreferences(BaseModel):
    model_config = ConfigDict(extra="allow")

    sensitivity: str | None = None
    quiet_hours: str | None = None
    channels: list[str] = Field(default_factory=list)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="allow")

    dashboard: DashboardPreferences = Field(default_factory=DashboardPreferences)
    naming: NamingPreferences = Field(default_factory=NamingPreferences)
    alerts: AlertPreferences = Field(default_factory=AlertPreferences)


class Note(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    content: str
    entity: str | None = None
    area: str | None = None
    scope: str | None = None  # "general" when not entity/area-scoped
    added: str | None = None
    source: NoteSource = "conversation"
    metadata: ItemMetadata = Field(default_factory=ItemMetadata)

    @model_validator(mode="before")
    @classmethod
    def _flatten_dict_scope(cls, data: Any) -> Any:
        """Accept Haiku reconciler output where ``scope`` is a dict.

        The scratchpad format uses ``scope: {entity: ..., area: ...}``
        and Haiku occasionally copies that shape into context.yaml
        Notes. Lift those fields up and coerce ``scope`` into the
        expected string so validation passes.
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("scope")
        if not isinstance(raw, dict):
            return data
        flat = dict(data)
        if not flat.get("entity") and isinstance(raw.get("entity"), str):
            flat["entity"] = raw["entity"]
        if not flat.get("area") and isinstance(raw.get("area"), str):
            flat["area"] = raw["area"]
        if raw.get("general"):
            flat["scope"] = "general"
        else:
            flat["scope"] = None
        return flat


class KnownIssue(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    description: str
    first_seen: str | None = None
    status: str = "active"
    evidence: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None
    user_acknowledged: bool = False


class Pattern(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    description: str
    confidence: float = 0.0
    first_observed: str | None = None
    last_confirmed: str | None = None
    source: str = "observation"
    exceptions: list[str] = Field(default_factory=list)


class RejectedSuggestion(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    suggestion: str
    reason: str | None = None
    date: str | None = None


class Claim(BaseModel):
    model_config = ConfigDict(extra="allow")

    content: str
    source: str
    evidence: str | None = None
    date: str | None = None


class Conflict(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    subject: dict[str, Any] = Field(default_factory=dict)
    claim_a: Claim | None = None
    claim_b: Claim | None = None
    status: str = "pending_review"
    resolution: dict[str, Any] | None = None


class EntityBaseline(BaseModel):
    model_config = ConfigDict(extra="allow")

    entity: str
    metric: str
    avg: float
    stddev: float
    last_calculated: str | None = None


class EnergyBaseline(BaseModel):
    model_config = ConfigDict(extra="allow")

    daily_kwh_avg: float | None = None
    daily_kwh_stddev: float | None = None
    last_calculated: str | None = None
    by_period: dict[str, Any] = Field(default_factory=dict)


class Baselines(BaseModel):
    model_config = ConfigDict(extra="allow")

    energy: EnergyBaseline | None = None
    entities: list[EntityBaseline] = Field(default_factory=list)


class NotificationSuppression(BaseModel):
    """A rule that suppresses specific notification types.

    Used by the notifier to skip alerts the user explicitly asked to
    stop receiving. Checked before every send — pure Python, no LLM.
    """

    model_config = ConfigDict(extra="allow")

    type: str = Field(
        description=(
            "Notification type to suppress: 'stale_automation', "
            "'unavailable', 'anomaly', 'sync_conflict', or '*' for all."
        ),
    )
    entity: str | None = Field(
        default=None,
        description="Specific entity_id to suppress. Null = suppress all of this type.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the user suppressed this (for Memory tab display).",
    )
    added: str | None = None


class PendingAction(BaseModel):
    """A monitor finding waiting to be shown in the catch-up banner.

    Lifecycle is managed by ``mylo.monitor.findings``: keyed by
    (type, entity_id), capped, auto-resolved when the condition
    clears, expired after 48h. Dismissing applies a cooldown.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str  # duration_anomaly, while_away, anomaly, unavailable, stale_automation
    entity_id: str
    title: str
    message: str
    detected_at: str  # ISO timestamp
    last_seen: str | None = None  # refreshed on every re-detection
    confidence: float = 1.0  # orders the banner; lowest evicted at cap
    resolved: bool = False  # legacy field — kept for migration detection


class FindingCooldown(BaseModel):
    """A dismissed finding's snooze — detectors skip this
    (type, entity) pair until ``until``."""

    model_config = ConfigDict(extra="allow")

    type: str
    entity_id: str
    until: str  # ISO timestamp


class Suggestion(BaseModel):
    """A proactive suggestion Mylo made, with outcome tracking.

    Used by the suggestion engine to decide when to stop suggesting
    (user keeps rejecting) vs when to propose an automation (user
    keeps accepting the same manual action).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str  # while_away, duration_anomaly (legacy: on_while_away, unlocked_too_long, device_running_long)
    entity_id: str
    description: str
    times_suggested: int = 0
    times_accepted: int = 0
    times_rejected: int = 0
    times_ignored: int = 0
    last_suggested: str | None = None
    automated: bool = False  # True once an automation was created for this
    automation_id: str | None = None


class MemoryFile(BaseModel):
    """Root of ``context.yaml``. See spec §3.2."""

    model_config = ConfigDict(extra="allow")

    version: int = 2
    last_sync: str | None = None
    sync_hash: str | None = None

    household: Household = Field(default_factory=Household)
    preferences: Preferences = Field(default_factory=Preferences)
    notes: list[Note] = Field(default_factory=list)
    known_issues: list[KnownIssue] = Field(default_factory=list)
    patterns: list[Pattern] = Field(default_factory=list)
    rejected: list[RejectedSuggestion] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    monitored_entities: list[str] = Field(default_factory=list)
    notification_suppressions: list[NotificationSuppression] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    pending_actions: list[PendingAction] = Field(default_factory=list)
    finding_cooldowns: list[FindingCooldown] = Field(default_factory=list)
    baselines: Baselines = Field(default_factory=Baselines)

    def pending_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.status == "pending_review"]

    def is_notification_suppressed(
        self, notification_type: str, entity_id: str | None = None
    ) -> bool:
        """Check if a notification should be suppressed."""
        for rule in self.notification_suppressions:
            if rule.type == "*":
                return True
            if rule.type != notification_type:
                continue
            if rule.entity is None:
                return True
            if entity_id and rule.entity == entity_id:
                return True
        return False


def empty_memory() -> MemoryFile:
    """Return an empty but valid memory document — used on first run."""
    return MemoryFile(
        version=2,
        last_sync=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )
