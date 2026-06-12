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

"""Profile-gated detectors — the learned replacement for the old
fixed-threshold suggestion rules.

Every check here must pass the entity's own learned profile before
it can fire:

* **duration_anomaly** — an entity has been in its active state
  longer than its learned threshold (its own all-time max with a
  margin, and a multiple of its typical p95 duration).
* **while_away** — an entity is on while everyone is away AND its
  profile says that's rare for this entity.

Entities without enough history stay silent — quiet until confident.
This module also does the learning side of the while-away check:
each sweep that runs during away time records one sample per
light/switch into the profiles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mylo.ha.states import get_all_states
from mylo.ha.ws_client import HaWsClient
from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile
from mylo.monitor.profiles import (
    ACTIVE_STATES,
    EntityProfile,
    ProfileSet,
    away_eligible,
    confidence,
    duration_eligible,
    duration_threshold_s,
    is_rarely_on_while_away,
)
from mylo.monitor.suggestions import (
    SuggestionAction,
    _should_offer_automation,
    _should_skip,
)

log = get_logger(__name__)

# Domains sampled and checked for while-away behavior.
AWAY_DOMAINS = ("light", "switch")

_TURN_OFF_SERVICES: dict[str, str] = {
    "light": "light.turn_off",
    "switch": "switch.turn_off",
    "fan": "fan.turn_off",
    "media_player": "media_player.turn_off",
    "lock": "lock.lock",
    "cover": "cover.close_cover",
}

# How to phrase the active state in messages ("on" for most domains).
_STATE_VERBS: dict[str, str] = {
    "lock": "unlocked",
    "cover": "open",
    "binary_sensor": "open",
}


async def run_learned_checks(
    *,
    ws_client: HaWsClient,
    memory: MemoryFile,
    profiles: ProfileSet,
) -> list[SuggestionAction]:
    """Run duration + while-away checks against learned profiles.

    Mutates ``profiles`` (away samples) — the caller is responsible
    for saving the profile set afterwards.
    """
    states = await get_all_states(ws_client)
    now = datetime.now(UTC)
    actions: list[SuggestionAction] = []

    person_states = {
        eid: s.get("state", "") for eid, s in states.items() if eid.startswith("person.")
    }
    anyone_home = any(s == "home" for s in person_states.values()) if person_states else True
    away = bool(person_states) and not anyone_home

    if away:
        _record_away_samples(profiles, states, now)

    # 1. Duration anomalies.
    for entity_id, state in states.items():
        domain = entity_id.split(".", 1)[0]
        active = ACTIVE_STATES.get(domain)
        if active is None or state.get("state") != active:
            continue
        profile = profiles.entities.get(entity_id)
        if profile is None or not duration_eligible(profile):
            continue

        duration_s = _seconds_in_state(state, now)
        if duration_s is None:
            continue
        device_class = state.get("attributes", {}).get("device_class")
        threshold = duration_threshold_s(profile, device_class=device_class)
        if threshold <= 0 or duration_s <= threshold:
            continue

        if memory.is_notification_suppressed("duration_anomaly", entity_id):
            continue
        sid = f"duration_anomaly_{entity_id}"
        if _should_skip(sid, memory):
            continue

        friendly = state.get("attributes", {}).get("friendly_name", entity_id)
        verb = _STATE_VERBS.get(domain, "on")
        actions.append(
            SuggestionAction(
                suggestion_id=sid,
                type="duration_anomaly",
                entity_id=entity_id,
                title=f"{friendly} {verb} unusually long",
                message=(
                    f"{friendly} has been {verb} for {_fmt_duration(duration_s)} — "
                    f"the longest you've left it before is "
                    f"{_fmt_duration(profile.max_duration_s)}."
                ),
                accept_service=_TURN_OFF_SERVICES.get(domain),
                accept_target=({"entity_id": entity_id} if domain in _TURN_OFF_SERVICES else {}),
                confidence=confidence(profile),
                offer_automation=_should_offer_automation(sid, memory),
            )
        )

    # 2. On while away — only when that's rare for this entity.
    if away:
        for entity_id, state in states.items():
            domain = entity_id.split(".", 1)[0]
            if domain not in AWAY_DOMAINS or state.get("state") != "on":
                continue
            profile = profiles.entities.get(entity_id)
            if profile is None or not away_eligible(profile):
                continue
            if not is_rarely_on_while_away(profile):
                continue
            if memory.is_notification_suppressed("while_away", entity_id):
                continue
            sid = f"while_away_{entity_id}"
            if _should_skip(sid, memory):
                continue

            friendly = state.get("attributes", {}).get("friendly_name", entity_id)
            actions.append(
                SuggestionAction(
                    suggestion_id=sid,
                    type="while_away",
                    entity_id=entity_id,
                    title=f"{friendly} is on while you're away",
                    message=(
                        f"{friendly} is on and nobody is home — you don't usually "
                        f"leave it on when you're out. Want me to turn it off?"
                    ),
                    accept_service=f"{domain}.turn_off",
                    accept_target={"entity_id": entity_id},
                    confidence=confidence(profile),
                    offer_automation=_should_offer_automation(sid, memory),
                )
            )

    return actions


def _record_away_samples(
    profiles: ProfileSet,
    states: dict[str, dict[str, Any]],
    now: datetime,
) -> None:
    """One hourly observation of away-time behavior per entity."""
    for entity_id, state in states.items():
        domain = entity_id.split(".", 1)[0]
        if domain not in AWAY_DOMAINS:
            continue
        s = state.get("state", "")
        if s in ("unavailable", "unknown"):
            continue
        profile = profiles.entities.get(entity_id)
        if profile is None:
            profile = EntityProfile(
                entity_id=entity_id, first_seen=now.isoformat(timespec="seconds")
            )
            profiles.entities[entity_id] = profile
        profile.away_samples += 1
        if s == "on":
            profile.on_while_away_samples += 1


def _seconds_in_state(state: dict[str, Any], now: datetime) -> float | None:
    last_changed = state.get("last_changed")
    if not last_changed:
        return None
    try:
        changed_dt = datetime.fromisoformat(str(last_changed).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (now - changed_dt).total_seconds()


def _fmt_duration(seconds: float) -> str:
    hours = seconds / 3600
    if hours >= 1:
        text = f"{hours:.1f}".rstrip("0").rstrip(".")
        return f"{text}h"
    return f"{int(seconds / 60)}m"
