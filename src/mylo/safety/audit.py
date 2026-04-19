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

"""Append-only audit log for agent actions.

Spec §5.6. Every tool invocation produces an :class:`AuditEntry` written as
one JSON object per line to ``/<mylo_data_dir>/audit/YYYY-MM.log``. The file
is opened ``a`` each write — processes can never modify an earlier entry
through normal API use.

We deliberately avoid a rotation/compaction scheme in v1: a monthly file
caps unbounded growth, and the append-only invariant is what earns trust.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mylo.logging_setup import get_logger

log = get_logger(__name__)

ResultKind = Literal["success", "failure", "rolled_back", "denied"]


@dataclass(slots=True)
class AuditEntry:
    """One row in the audit log. See spec §5.6 for field semantics."""

    timestamp: str  # ISO 8601 UTC
    conversation_id: str
    tool_name: str
    tier: int
    params: dict[str, Any]
    dry_run: bool
    user_approved: bool
    result: ResultKind
    details: dict[str, Any] = field(default_factory=dict)
    rollback_performed: bool = False
    file_backup_path: str | None = None

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))


def _monthly_filename(now: datetime | None = None) -> str:
    n = now or datetime.now(UTC)
    return f"{n.year:04d}-{n.month:02d}.log"


class AuditLogger:
    """Append-only JSON Lines writer.

    Instance-level lock serializes writes so two concurrent tool calls
    can't interleave partial lines. A single instance is expected per
    process — store it on whatever DI root you have.
    """

    def __init__(self, mylo_data_dir: Path) -> None:
        self._dir = mylo_data_dir / "audit"
        self._lock = asyncio.Lock()

    async def write(self, entry: AuditEntry) -> None:
        async with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / _monthly_filename()
            line = entry.to_json_line() + "\n"
            # Block on disk IO in a thread so we don't stall the loop on a
            # slow SD card. Keep the lock held though — ordering matters
            # more than throughput here.
            await asyncio.to_thread(_append, path, line)

    def read_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` entries across the current and
        prior month. For the Activity tab (M12); tolerant of missing files
        and malformed lines so a corrupt row never blinds the viewer.
        """
        rows: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        months: list[datetime] = [now]
        # Previous month.
        if now.month == 1:
            months.append(now.replace(year=now.year - 1, month=12, day=1))
        else:
            months.append(now.replace(month=now.month - 1, day=1))

        for m in months:
            path = self._dir / _monthly_filename(m)
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rows.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return rows[:limit]


def _append(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def make_entry(
    *,
    conversation_id: str,
    tool_name: str,
    tier: int,
    params: dict[str, Any],
    dry_run: bool,
    user_approved: bool,
    result: ResultKind,
    details: dict[str, Any] | None = None,
    rollback_performed: bool = False,
    file_backup_path: str | None = None,
) -> AuditEntry:
    return AuditEntry(
        timestamp=datetime.now(UTC).isoformat(),
        conversation_id=conversation_id,
        tool_name=tool_name,
        tier=tier,
        params=params,
        dry_run=dry_run,
        user_approved=user_approved,
        result=result,
        details=details or {},
        rollback_performed=rollback_performed,
        file_backup_path=file_backup_path,
    )
