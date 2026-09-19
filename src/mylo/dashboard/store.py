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

"""In-memory store for validated dashboard plans awaiting Apply.

Plans live for one hour. There is no persistence: if the add-on
restarts between plan and apply, ``apply_dashboard_plan`` returns
``plan_not_found`` and the model re-plans.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from mylo.dashboard.plan import DashboardPlan


@dataclass(slots=True)
class _Entry:
    plan: DashboardPlan
    expires_at: float


class PlanStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        capacity: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(4)

    def put(self, plan: DashboardPlan) -> None:
        self._expire()
        self._entries[plan.plan_id] = _Entry(plan=plan, expires_at=self._clock() + self._ttl)
        while len(self._entries) > self._capacity:
            oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
            del self._entries[oldest]

    def get(self, plan_id: str) -> DashboardPlan | None:
        self._expire()
        entry = self._entries.get(plan_id)
        return entry.plan if entry is not None else None

    def remove(self, plan_id: str) -> None:
        self._entries.pop(plan_id, None)

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[key]
