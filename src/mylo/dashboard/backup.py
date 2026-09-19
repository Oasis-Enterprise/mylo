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

"""Pre-apply snapshots of a dashboard config.

Storage-mode dashboards have no file for the rollback machinery in
``mylo.files`` to protect, so the executor writes its own JSON
snapshot before every save and keeps the newest ``keep`` per dashboard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mylo.files.manager import atomic_write

BACKUP_DIRNAME = "dashboard_backups"


def write_backup(
    mylo_data_dir: Path,
    dashboard_id: str | None,
    config: dict[str, Any],
    *,
    keep: int = 20,
    now: datetime | None = None,
) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
    folder = mylo_data_dir / BACKUP_DIRNAME / (dashboard_id or "default")
    path = folder / f"{stamp}.json"
    atomic_write(path, json.dumps(config, indent=2, sort_keys=True))
    existing = sorted(folder.glob("*.json"))
    for old in existing[: max(0, len(existing) - keep)]:
        old.unlink(missing_ok=True)
    return path
