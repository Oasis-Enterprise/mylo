"""Atomic file writes.

Every config file change lands through :func:`atomic_write`: write to a
sibling tempfile, fsync, then ``os.replace`` onto the target. This is
POSIX-atomic — a reader (HA, a backup tool) always sees either the old
file or the fully-written new one, never a torn half.

Backups are taken by :mod:`mylo.files.backup` just before the replace
step; the backup path is returned so it can be recorded in the audit log.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically.

    Creates parent directories as needed. The temp file lives in the same
    directory as ``path`` so ``os.replace`` stays on one filesystem (it'd
    error across filesystems). Temp is removed automatically if the write
    fails before the replace.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".mylo-tmp")
    try:
        with tmp.open("w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def read_text(path: Path, *, encoding: str = "utf-8") -> str:
    return Path(path).read_text(encoding=encoding)


def exists(path: Path) -> bool:
    return Path(path).exists()
