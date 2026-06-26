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

"""Persistent monthly spend ledger.

Session cost is tracked client-side and resets per session; the monthly
budget needs server-side state that survives restarts and rolls over on
the calendar month. This keeps a single tiny JSON file under the Mylo
data dir holding the current month and its accumulated estimated spend.

Deliberately minimal: one month of state, not a rotating history. When
the month changes the running total resets to zero on the next write —
no cron, no compaction. It's an *estimate* (derived from token counts
times the cost table), so a corrupt or missing file just resets to zero rather
than failing a chat turn.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from mylo.logging_setup import get_logger

log = get_logger(__name__)

_LEDGER_FILENAME = "usage_ledger.json"


def _month_key(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m")


class UsageLedger:
    """Tracks estimated USD spend for the current calendar month."""

    def __init__(self, mylo_data_dir: Path) -> None:
        self._path = mylo_data_dir / _LEDGER_FILENAME

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt ledger must never break a chat turn — treat as empty.
            log.warning("usage_ledger.unreadable_resetting", error=str(exc))
            return {}
        return data if isinstance(data, dict) else {}

    def month_spent(self, now: datetime | None = None) -> float:
        """Estimated spend for the current month so far (0.0 after rollover)."""
        data = self._read()
        if data.get("month") != _month_key(now):
            return 0.0
        try:
            return float(data.get("spent_usd", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def record(self, usd: float, now: datetime | None = None) -> float:
        """Add ``usd`` to the current month's total and persist. Returns the
        new month-to-date total.

        Rolls over automatically: if the stored month isn't the current
        one, the total restarts from this charge.
        """
        if usd < 0:
            usd = 0.0
        new_total = self.month_spent(now) + usd
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"month": _month_key(now), "spent_usd": round(new_total, 6)}),
                encoding="utf-8",
            )
        except OSError as exc:
            # Persisting is best-effort; a failed write shouldn't kill the
            # turn (we've already answered the user).
            log.warning("usage_ledger.unwritable", error=str(exc))
        return new_total
