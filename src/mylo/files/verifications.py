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

"""Background verification outcomes.

Optimistic writes (see :func:`mylo.files.rollback.apply_optimistic_reload_all`)
return before Home Assistant has reloaded. This log records each such write
as ``pending`` and its later outcome, persisted under the Mylo data dir so
the panel (via ``/api/status``) and the model (via the system prompt) can
tell the user what actually happened.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mylo.files.manager import atomic_write
from mylo.logging_setup import get_logger

log = get_logger(__name__)

VERIFICATIONS_FILENAME = "verifications.json"
STATUSES: tuple[str, ...] = ("pending", "verified", "failed", "rolled_back")


@dataclass(slots=True)
class Verification:
    id: str
    tool: str
    target: str
    status: str
    message: str
    conversation_id: str
    requested_at: str
    completed_at: str | None = None
    acknowledged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VerificationLog:
    """Persisted, capacity-bounded list of verifications."""

    def __init__(
        self,
        path: Path,
        *,
        capacity: int = 50,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._capacity = capacity
        self._clock = clock or (lambda: datetime.now(UTC))
        self._items, reaped = self._load()
        if reaped:
            self._save()

    # ── persistence ──────────────────────────────────────────────────────

    def _now_iso(self) -> str:
        return self._clock().replace(microsecond=0).isoformat()

    def _load(self) -> tuple[list[Verification], bool]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], False
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("verifications.unreadable_resetting", error=str(exc))
            return [], False
        items: list[Verification] = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                items.append(Verification(**entry))
            except TypeError:
                continue
        # A pending item with no completed_at means Mylo was killed or
        # restarted mid-verification — the background task that would have
        # completed it is gone, so it would otherwise stay "pending"
        # forever, blocking the model from ever reporting an outcome for it.
        reaped = False
        now = self._now_iso()
        for v in items:
            if v.status == "pending" and v.completed_at is None:
                v.status = "failed"
                v.message = "Mylo restarted before verification finished"
                v.completed_at = now
                reaped = True
        return items[-self._capacity :], reaped

    def _save(self) -> None:
        self._items = self._items[-self._capacity :]
        try:
            atomic_write(self._path, json.dumps([v.to_dict() for v in self._items], indent=1))
        except OSError as exc:
            # Never let bookkeeping fail a write; memory stays authoritative.
            log.warning("verifications.write_failed", error=str(exc))

    # ── API ──────────────────────────────────────────────────────────────

    def start(self, *, tool: str, target: str, conversation_id: str) -> Verification:
        v = Verification(
            id="vf_" + secrets.token_hex(6),
            tool=tool,
            target=target,
            status="pending",
            message="",
            conversation_id=conversation_id,
            requested_at=self._now_iso(),
        )
        self._items.append(v)
        self._save()
        return v

    def get(self, id: str) -> Verification | None:
        for v in self._items:
            if v.id == id:
                return v
        return None

    def complete(self, id: str, *, status: str, message: str) -> Verification | None:
        if status not in STATUSES or status == "pending":
            raise ValueError(f"invalid completion status {status!r}")
        v = self.get(id)
        if v is None:
            log.warning("verifications.unknown_id", id=id)
            return None
        v.status = status
        v.message = message
        v.completed_at = self._now_iso()
        self._save()
        return v

    def acknowledge(self, id: str) -> bool:
        v = self.get(id)
        if v is None:
            return False
        v.acknowledged = True
        self._save()
        return True

    def recent(self, *, hours: float = 24.0) -> list[Verification]:
        cutoff = self._clock() - timedelta(hours=hours)
        # Iterate newest-inserted-first so a stable sort on requested_at
        # breaks same-timestamp ties (second-resolution) newest-first too.
        out = [
            v for v in reversed(self._items) if _parse(v.completed_at or v.requested_at) >= cutoff
        ]
        out.sort(key=lambda v: v.requested_at, reverse=True)
        return out

    def unacknowledged(self) -> list[Verification]:
        return [v for v in self._items if v.completed_at is not None and not v.acknowledged]

    def for_prompt(self, *, hours: float = 24.0) -> list[Verification]:
        """Outcomes worth telling the model about: recent and not yet
        acknowledged by the user. Pending items have ``acknowledged=False``
        by construction, so they stay included until they complete."""
        return [v for v in self.recent(hours=hours) if not v.acknowledged]


def _parse(iso: str) -> datetime:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        # A corrupt/unparseable timestamp sorts oldest so it falls outside
        # every "recent" window instead of raising through the caller.
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def render_verifications(items: list[Verification], *, timezone: str | None = None) -> str:
    """One line per outcome for the system prompt; empty string if none."""
    if not items:
        return ""
    tz = ZoneInfo(timezone) if timezone else UTC
    lines = [
        "VERIFICATION OUTCOMES the user has not dismissed (last 24h) — report each "
        "completed one before anything else; a pending one only if asked:"
    ]
    for v in items:
        when = _parse(v.completed_at or v.requested_at).astimezone(tz).strftime("%H:%M")
        if v.status == "pending":
            outcome = "pending"
        elif v.status == "verified":
            outcome = "verified"
        else:
            outcome = f"{v.status.upper().replace('_', ' ')}: {v.message}"
        lines.append(f"- {when} {v.tool} {v.target} — {outcome}")
    return "\n".join(lines)
