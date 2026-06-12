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

"""Suggestion support primitives.

This module no longer generates suggestions itself. It provides:

* ``SuggestionAction`` — the shared shape that detectors emit.
* Outcome tracking — ``record_suggestion`` / ``record_outcome``
  persist how often a suggestion was accepted, rejected, or ignored
  so the system can silence patterns the user dislikes and eventually
  propose automations.
* Rejection-based silencing — ``_should_skip`` gates detectors on
  per-suggestion rejection history.
* Automation proposals — ``_should_offer_automation`` signals when
  a pattern has been accepted enough times to warrant an automation.

Detection now lives in ``mylo.monitor.detectors``, gated by learned
profiles (``mylo.monitor.profiles``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile, Suggestion

log = get_logger(__name__)

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
