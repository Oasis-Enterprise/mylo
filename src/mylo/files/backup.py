"""Timestamped backups for every config write.

Structure per spec §4.14::

    /config/.mylo/backups/
      automations.yaml/
        2026-04-14T14:30:00.yaml
        2026-04-14T15:45:00.yaml
      packages/agent/kitchen/automations.yaml/
        2026-04-14T14:30:00.yaml

The backup directory mirrors the relative path of the backed-up file with
a trailing slash (so "foo.yaml" becomes "foo.yaml/<timestamp>.yaml").
Keeps per-file history discoverable by humans. Rotated to the 10 most
recent per file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

KEEP_PER_FILE = 10


@dataclass(slots=True, frozen=True)
class BackupHandle:
    """Return value from :func:`take_backup` — caller records this in audit."""

    source: Path
    backup_path: Path | None  # None if source didn't exist yet (first write)


def take_backup(
    source: Path,
    config_dir: Path,
    mylo_data_dir: Path,
) -> BackupHandle:
    """Copy ``source`` to a timestamped path under
    ``{mylo_data_dir}/backups/<relpath>/<ts>.yaml``.

    No-op if ``source`` doesn't exist yet (first-write case). Rotation
    trims to the most recent :data:`KEEP_PER_FILE` copies.
    """
    source = Path(source)
    config_dir = Path(config_dir).resolve()
    if not source.exists():
        return BackupHandle(source=source, backup_path=None)

    src_resolved = source.resolve()
    try:
        rel = src_resolved.relative_to(config_dir)
    except ValueError:
        rel = Path(src_resolved.name)

    backup_root = mylo_data_dir / "backups" / rel
    backup_root.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    target = backup_root / f"{ts}{source.suffix or '.bak'}"
    target.write_bytes(src_resolved.read_bytes())

    _rotate(backup_root, keep=KEEP_PER_FILE)
    return BackupHandle(source=source, backup_path=target)


def list_backups(source: Path, config_dir: Path, mylo_data_dir: Path) -> list[Path]:
    """List existing backups for a source file, newest first."""
    source = Path(source).resolve()
    try:
        rel = source.relative_to(Path(config_dir).resolve())
    except ValueError:
        rel = Path(source.name)
    backup_root = mylo_data_dir / "backups" / rel
    if not backup_root.exists():
        return []
    return sorted(backup_root.iterdir(), key=lambda p: p.name, reverse=True)


def _rotate(root: Path, *, keep: int) -> None:
    files = sorted(root.iterdir(), key=lambda p: p.name, reverse=True)
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)
