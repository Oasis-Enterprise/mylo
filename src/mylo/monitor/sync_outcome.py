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

"""Turn a nightly reconcile result into user-visible state."""

from __future__ import annotations

from datetime import datetime

from mylo.memory.schema import MemoryFile
from mylo.monitor import findings as findings_store

SYNC_NOOP_PREFIX = "no new scratchpad entries"
SYNC_FAILED_ID = "mylo_sync_failed"


def apply_sync_outcome(memory: MemoryFile, *, summary: str, failed: bool, now: datetime) -> bool:
    """Record a failed sync (error field + finding) or clear a prior failure.

    Returns True when ``memory`` changed and should be saved.
    """
    if failed:
        memory.last_sync_error = summary
        memory.last_sync_attempt = now.replace(microsecond=0).isoformat()
        findings_store.upsert_finding(
            memory,
            finding_id=SYNC_FAILED_ID,
            finding_type="sync_failed",
            entity_id="",
            title="Memory sync failed",
            message=f"{summary}. Your notes are kept and Mylo will retry tonight.",
            confidence=1.0,
            now=now,
        )
        return True
    changed = memory.last_sync_error is not None
    memory.last_sync_error = None
    removed = findings_store.remove_finding(memory, SYNC_FAILED_ID)
    return changed or removed
