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

"""Proactive suggestion engine.

Analyzes current state + transition history to generate suggestions:

* **on_while_away** — lights/switches on when nobody is home.
  "Kitchen lights are on and you appear to be away. Turn them off?"
* **unlocked_too_long** — lock entities in "unlocked" state for
  longer than a threshold.
* **device_running_long** — switches/media_players that have been
  "on" continuously for an unusual duration.

Suggestions are tracked in memory (``MemoryFile.suggestions``).
Each suggestion records how many times it was made, accepted,
rejected, or ignored. After 3+ acceptances of the same pattern
without a rejection, Phase 4 will offer to create an automation.

The engine runs during the hourly sweep but produces
``SuggestionAction`` objects instead of raw notification dicts —
the caller decides whether to send a notification, and with what
action buttons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mylo.ha.states import get_all_states
from mylo.ha.ws_client import HaWsClient
from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile, Suggestion

log = get_logger(__name__)

# How long a lock must be unlocked before we suggest re-locking.
UNLOCK_THRESHOLD_MINUTES = 30

# How long a device must be continuously "on" before we flag it.
DEVICE_ON_THRESHOLD_HOURS = 4

# After this many rejections/ignores with no acceptances, stop suggesting.
MAX_REJECTIONS_BEFORE_SILENCE = 5

# After this many acceptances, offer to create an automation (Phase 4).
ACCEPTANCES_BEFORE_AUTOMATION = 3


@dataclass(slots=True)
class SuggestionAction:
    """A proactive suggestion ready to be sent as a notification."""

    suggestion_id: str
    type: str
    entity_id: str
    title: str
    message: str
    severity: str = "normal"
    # Service to call if user accepts (e.g. light.turn_off).
    accept_service: str | None = None
    accept_target: dict[str, Any] = field(default_factory=dict)
    # True when this pattern has been accepted enough times to
    # offer automation creation.
    offer_automation: bool = False
    # Profile confidence (0..1) — orders findings in the banner.
    confidence: float = 1.0


async def run_suggestions(
    *,
    ws_client: HaWsClient,
    memory: MemoryFile,
) -> list[SuggestionAction]:
    """Generate proactive suggestions from current state.

    Called during the hourly sweep. Returns actions the notifier
    should present to the user.
    """
    states = await get_all_states(ws_client)
    actions: list[SuggestionAction] = []

    # Check person entities for presence.
    person_states = {
        eid: s.get("state", "") for eid, s in states.items() if eid.startswith("person.")
    }
    anyone_home = any(s == "home" for s in person_states.values()) if person_states else True

    # 1. Lights/switches on while away.
    if not anyone_home:
        for entity_id, state in states.items():
            domain = entity_id.split(".", 1)[0]
            if domain not in ("light", "switch"):
                continue
            if state.get("state") != "on":
                continue

            # Skip entities the user has suppressed via notification
            # filters — includes infrastructure devices (network
            # switches, cameras, servers) that are always on.
            if memory.is_notification_suppressed("on_while_away", entity_id):
                continue

            friendly = state.get("attributes", {}).get("friendly_name", entity_id)
            sid = f"on_while_away_{entity_id}"

            if _should_skip(sid, memory):
                continue

            actions.append(
                SuggestionAction(
                    suggestion_id=sid,
                    type="on_while_away",
                    entity_id=entity_id,
                    title=f"{friendly} is on while you're away",
                    message=f"{friendly} is still on and nobody is home. Want me to turn it off?",
                    accept_service=f"{domain}.turn_off",
                    accept_target={"entity_id": entity_id},
                    offer_automation=_should_offer_automation(sid, memory),
                )
            )

    # 2. Locks unlocked too long.
    for entity_id, state in states.items():
        if not entity_id.startswith("lock."):
            continue
        if state.get("state") != "unlocked":
            continue
        if memory.is_notification_suppressed("unlocked_too_long", entity_id):
            continue
        last_changed = state.get("last_changed")
        if not last_changed:
            continue
        try:
            changed_dt = datetime.fromisoformat(str(last_changed).replace("Z", "+00:00"))
            delta = datetime.now(UTC) - changed_dt
            if delta < timedelta(minutes=UNLOCK_THRESHOLD_MINUTES):
                continue
        except (ValueError, TypeError):
            continue

        friendly = state.get("attributes", {}).get("friendly_name", entity_id)
        sid = f"unlocked_too_long_{entity_id}"

        if _should_skip(sid, memory):
            continue

        minutes = int(delta.total_seconds() / 60)
        actions.append(
            SuggestionAction(
                suggestion_id=sid,
                type="unlocked_too_long",
                entity_id=entity_id,
                title=f"{friendly} unlocked for {minutes} minutes",
                message=f"{friendly} has been unlocked for {minutes} minutes. Want me to lock it?",
                severity="normal",
                accept_service="lock.lock",
                accept_target={"entity_id": entity_id},
                offer_automation=_should_offer_automation(sid, memory),
            )
        )

    # 3. Devices running too long.
    for entity_id, state in states.items():
        domain = entity_id.split(".", 1)[0]
        if domain not in ("switch", "media_player", "fan"):
            continue
        if state.get("state") != "on":
            continue
        if memory.is_notification_suppressed("device_running_long", entity_id):
            continue
        last_changed = state.get("last_changed")
        if not last_changed:
            continue
        try:
            changed_dt = datetime.fromisoformat(str(last_changed).replace("Z", "+00:00"))
            delta = datetime.now(UTC) - changed_dt
            if delta < timedelta(hours=DEVICE_ON_THRESHOLD_HOURS):
                continue
        except (ValueError, TypeError):
            continue

        friendly = state.get("attributes", {}).get("friendly_name", entity_id)
        sid = f"device_running_long_{entity_id}"

        if _should_skip(sid, memory):
            continue

        hours = round(delta.total_seconds() / 3600, 1)
        actions.append(
            SuggestionAction(
                suggestion_id=sid,
                type="device_running_long",
                entity_id=entity_id,
                title=f"{friendly} has been on for {hours}h",
                message=f"{friendly} has been running for {hours} hours. Is this intentional?",
                severity="low",
                accept_service=f"{domain}.turn_off",
                accept_target={"entity_id": entity_id},
                offer_automation=_should_offer_automation(sid, memory),
            )
        )

    return actions


def record_suggestion(
    memory: MemoryFile,
    suggestion_id: str,
    suggestion_type: str,
    entity_id: str,
    description: str,
) -> None:
    """Record that a suggestion was made (increment times_suggested)."""
    existing = _find_suggestion(memory, suggestion_id)
    if existing:
        existing.times_suggested += 1
        existing.last_suggested = datetime.now(UTC).isoformat(timespec="seconds")
    else:
        memory.suggestions.append(
            Suggestion(
                id=suggestion_id,
                type=suggestion_type,
                entity_id=entity_id,
                description=description,
                times_suggested=1,
                last_suggested=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        )


def record_outcome(
    memory: MemoryFile,
    suggestion_id: str,
    outcome: str,  # "accepted" | "rejected" | "ignored"
) -> None:
    """Record the user's response to a suggestion."""
    existing = _find_suggestion(memory, suggestion_id)
    if not existing:
        return
    if outcome == "accepted":
        existing.times_accepted += 1
    elif outcome == "rejected":
        existing.times_rejected += 1
    elif outcome == "ignored":
        existing.times_ignored += 1


def _find_suggestion(memory: MemoryFile, suggestion_id: str) -> Suggestion | None:
    for s in memory.suggestions:
        if s.id == suggestion_id:
            return s
    return None


def _should_skip(suggestion_id: str, memory: MemoryFile) -> bool:
    """Don't suggest if user keeps rejecting or if already automated."""
    existing = _find_suggestion(memory, suggestion_id)
    if not existing:
        return False
    if existing.automated:
        return True
    # Too many rejections/ignores with no acceptances = stop.
    negative = existing.times_rejected + existing.times_ignored
    return negative >= MAX_REJECTIONS_BEFORE_SILENCE and existing.times_accepted == 0


def _should_offer_automation(suggestion_id: str, memory: MemoryFile) -> bool:
    """After N acceptances, suggest creating an automation."""
    existing = _find_suggestion(memory, suggestion_id)
    if not existing:
        return False
    if existing.automated:
        return False
    return existing.times_accepted >= ACCEPTANCES_BEFORE_AUTOMATION
