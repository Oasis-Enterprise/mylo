from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mylo.dashboard.backup import write_backup


def test_backup_written_and_rotated(tmp_path: Path) -> None:
    base = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
    paths = [
        write_backup(tmp_path, None, {"views": [{"n": i}]}, keep=3, now=base + timedelta(seconds=i))
        for i in range(5)
    ]
    folder = tmp_path / "dashboard_backups" / "default"
    remaining = sorted(folder.glob("*.json"))
    assert len(remaining) == 3
    assert remaining[-1] == paths[-1]
    assert json.loads(paths[-1].read_text())["views"] == [{"n": 4}]
    assert not paths[0].exists()


def test_backup_uses_dashboard_id_folder(tmp_path: Path) -> None:
    p = write_backup(tmp_path, "tablet", {"views": []})
    assert p.parent == tmp_path / "dashboard_backups" / "tablet"
