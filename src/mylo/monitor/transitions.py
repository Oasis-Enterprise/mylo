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

"""State transition logger for behavioral pattern detection.

The hourly sweep sees *current* state. This module sees *changes* —
"light turned on at 6:43pm", "person.maxwell changed to away at
8:12am". These transitions feed the pattern detector (Phase 3)
which looks for recurring sequences over days/weeks.

Architecture: subscribe to HA's ``state_changed`` events for a set
of watched domains. Each transition is appended to a rolling log
file at ``{mylo_data_dir}/transitions.jsonl``. The file is capped
by age (14 days) not size — old entries are pruned on each write.

The logger is lightweight by design:
- No LLM calls
- No complex processing — just append + prune
- Only logs meaningful transitions (state changes, not attribute updates)
- Only watches domains that matter for behavioral patterns
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mylo.logging_setup import get_logger

log = get_logger(__name__)

# Domains worth tracking for behavioral patterns.
WATCHED_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "lock",
        "cover",
        "person",
        "binary_sensor",
        "climate",
        "media_player",
        "fan",
    }
)

# How long to keep transition data.
RETENTION_DAYS = 14


@dataclass(slots=True)
class Transition:
    """One state change event."""

    entity_id: str
    from_state: str
    to_state: str
    timestamp: str  # ISO 8601

    def to_dict(self) -> dict[str, str]:
        return {
            "entity_id": self.entity_id,
            "from": self.from_state,
            "to": self.to_state,
            "ts": self.timestamp,
        }


class TransitionLogger:
    """Append-only state-change logger with age-based pruning."""

    def __init__(self, mylo_data_dir: Path) -> None:
        self._path = mylo_data_dir / "transitions.jsonl"
        self._write_count = 0

    def record(self, event: dict[str, Any]) -> None:
        """Called from an HA state_changed event subscription.

        Filters to watched domains, extracts old/new state, appends
        to the log. Prunes stale entries every 100 writes.
        """
        entity_id = event.get("entity_id", "")
        domain = entity_id.split(".", 1)[0] if entity_id else ""
        if domain not in WATCHED_DOMAINS:
            return

        old_state = event.get("old_state") or {}
        new_state = event.get("new_state") or {}
        from_val = old_state.get("state", "")
        to_val = new_state.get("state", "")

        # Skip attribute-only updates (state didn't change).
        if from_val == to_val:
            return
        # Skip unavailable transitions (handled by hourly sweep).
        if to_val in ("unavailable", "unknown") or from_val in ("unavailable", "unknown"):
            return

        transition = Transition(
            entity_id=entity_id,
            from_state=from_val,
            to_state=to_val,
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        )

        self._append(transition)
        self._write_count += 1
        if self._write_count >= 100:
            self._prune()
            self._write_count = 0

    def read_recent(self, hours: int = 24) -> list[Transition]:
        """Read transitions from the last N hours."""
        if not self._path.exists():
            return []

        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        transitions: list[Transition] = []

        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = data.get("ts", "")
                    if ts >= cutoff:
                        transitions.append(
                            Transition(
                                entity_id=data.get("entity_id", ""),
                                from_state=data.get("from", ""),
                                to_state=data.get("to", ""),
                                timestamp=ts,
                            )
                        )
        except OSError as exc:
            log.warning("transitions.read_failed", error=str(exc))

        return transitions

    def read_for_entity(self, entity_id: str, days: int = 7) -> list[Transition]:
        """Read transitions for a specific entity over N days."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        results: list[Transition] = []

        if not self._path.exists():
            return results

        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("entity_id") != entity_id:
                        continue
                    ts = data.get("ts", "")
                    if ts >= cutoff:
                        results.append(
                            Transition(
                                entity_id=entity_id,
                                from_state=data.get("from", ""),
                                to_state=data.get("to", ""),
                                timestamp=ts,
                            )
                        )
        except OSError as exc:
            log.warning("transitions.read_failed", error=str(exc))

        return results

    def _append(self, transition: Transition) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(transition.to_dict()) + "\n")
        except OSError as exc:
            log.warning("transitions.write_failed", error=str(exc))

    def _prune(self) -> None:
        """Remove entries older than RETENTION_DAYS."""
        if not self._path.exists():
            return

        cutoff = (datetime.now(UTC) - timedelta(days=RETENTION_DAYS)).isoformat()
        kept: list[str] = []

        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("ts", "") >= cutoff:
                        kept.append(line)

            with self._path.open("w", encoding="utf-8") as f:
                for line in kept:
                    f.write(line + "\n")

            log.debug("transitions.pruned", kept=len(kept))
        except OSError as exc:
            log.warning("transitions.prune_failed", error=str(exc))
