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

"""Load and save ``context.yaml`` with versioned history.

Layout (spec §3.8)::

    /<mylo_data_dir>/
      context.yaml                  # current version
      history/
        context_2026-04-15.yaml
        context_2026-04-14.yaml
        ...
      changelog.yaml                # append-only, sync events

Reads are cheap and safe; writes snapshot the previous version into
history before replacing the current file. The changelog is
append-only text — we never rewrite prior entries.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mylo.files.manager import atomic_write, exists, read_text
from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile, empty_memory
from mylo.validators.yaml_parser import dump_yaml, load_yaml

log = get_logger(__name__)

HISTORY_RETENTION = 30  # daily snapshots kept


@dataclass(slots=True)
class MemoryStore:
    """Disk-backed memory with an in-memory cache.

    Call :meth:`load` once at startup, then :meth:`current` to read
    without hitting disk. :meth:`save` writes atomically and rotates
    the history directory.
    """

    mylo_data_dir: Path
    _cached: MemoryFile | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def path(self) -> Path:
        return self.mylo_data_dir / "context.yaml"

    @property
    def history_dir(self) -> Path:
        return self.mylo_data_dir / "history"

    @property
    def changelog_path(self) -> Path:
        return self.mylo_data_dir / "changelog.yaml"

    async def load(self) -> MemoryFile:
        """Load context.yaml from disk, creating an empty one if missing."""
        async with self._lock:
            if not exists(self.path):
                memory = empty_memory()
                await self._write_unlocked(memory, snapshot=False)
                self._cached = memory
                return memory
            raw = read_text(self.path)
            try:
                parsed = load_yaml(raw) or {}
            except Exception as exc:
                log.warning("memory.load_failed", error=str(exc))
                parsed = {}
            try:
                memory = MemoryFile.model_validate(parsed)
            except Exception as exc:
                log.warning(
                    "memory.schema_invalid",
                    error=str(exc),
                    note="falling back to empty memory so chat doesn't block",
                )
                memory = empty_memory()
            self._cached = memory
            return memory

    def current(self) -> MemoryFile:
        """Return the cached memory. Load first if not cached."""
        if self._cached is None:
            return empty_memory()
        return self._cached

    async def save(self, memory: MemoryFile, *, note: str = "") -> None:
        """Persist ``memory`` atomically, snapshot the prior version."""
        async with self._lock:
            await self._write_unlocked(memory, snapshot=True)
            self._cached = memory
            await self._append_changelog(note or "memory saved")

    async def _write_unlocked(self, memory: MemoryFile, *, snapshot: bool) -> None:
        if snapshot and exists(self.path):
            self.history_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
            dest = self.history_dir / f"context_{stamp}.yaml"
            dest.write_bytes(self.path.read_bytes())
            _rotate_history(self.history_dir, keep=HISTORY_RETENTION)

        text = dump_yaml(memory.model_dump(exclude_none=False))
        atomic_write(self.path, text)

    async def _append_changelog(self, message: str) -> None:
        self.mylo_data_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "message": message,
        }
        line = dump_yaml([entry]).strip() + "\n"
        with self.changelog_path.open("a", encoding="utf-8") as f:
            f.write(line)


def _rotate_history(root: Path, *, keep: int) -> None:
    files = sorted(root.iterdir(), key=lambda p: p.name, reverse=True)
    for stale in files[keep:]:
        with contextlib.suppress(OSError):
            stale.unlink()
