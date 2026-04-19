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

"""Tests for files.manager (atomic write), files.backup, files.diff."""

from __future__ import annotations

from pathlib import Path

from mylo.files.backup import list_backups, take_backup
from mylo.files.diff import Change, diff_structs, diff_yaml
from mylo.files.manager import atomic_write


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.yaml"
    atomic_write(target, "x: 1\n")
    assert target.read_text() == "x: 1\n"
    # No leftover tempfile.
    assert not any(p.name.endswith(".mylo-tmp") for p in target.parent.iterdir())


def test_atomic_write_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "file.yaml"
    atomic_write(target, "old\n")
    atomic_write(target, "new\n")
    assert target.read_text() == "new\n"


def test_take_backup_creates_timestamped_copy(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / "automations.yaml"
    target.write_text("x: 1\n")

    mylo_dir = tmp_path / ".mylo"
    handle = take_backup(target, config_dir, mylo_dir)
    assert handle.backup_path is not None
    assert handle.backup_path.read_text() == "x: 1\n"
    # Parent directory mirrors the source filename.
    assert handle.backup_path.parent.name == "automations.yaml"


def test_rotation_trims_to_keep_limit(tmp_path: Path) -> None:
    # Exercise the rotation logic directly — take_backup uses per-second
    # timestamps, which would collide within a single test run.
    from mylo.files import backup as backup_module

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / "file.yaml"
    target.write_text("v\n")
    mylo_dir = tmp_path / ".mylo"

    backup_root = mylo_dir / "backups" / "file.yaml"
    backup_root.mkdir(parents=True)
    for i in range(15):
        (backup_root / f"2026-04-14T00-00-{i:02d}.yaml").write_text(str(i))

    backup_module._rotate(backup_root, keep=backup_module.KEEP_PER_FILE)
    backups = list_backups(target, config_dir, mylo_dir)
    assert len(backups) == backup_module.KEEP_PER_FILE
    # Trimmed ones are the *oldest*; newest preserved.
    names = [p.name for p in backups]
    assert names[0].endswith("14.yaml")
    assert not any(n.endswith("00.yaml") for n in names)


def test_take_backup_noop_when_source_missing(tmp_path: Path) -> None:
    handle = take_backup(tmp_path / "missing.yaml", tmp_path, tmp_path / ".mylo")
    assert handle.backup_path is None


def test_diff_structs_added_removed_changed() -> None:
    old = {"a": 1, "b": 2, "c": 3}
    new = {"a": 1, "b": 20, "d": 4}
    result = diff_structs(old, new)
    by_path = {e.path: e for e in result.entries}
    assert by_path["b"].change is Change.CHANGED
    assert by_path["c"].change is Change.REMOVED
    assert by_path["d"].change is Change.ADDED
    assert "a" not in by_path  # unchanged
    assert result.summary == {"added": 1, "removed": 1, "changed": 1}


def test_diff_handles_nested() -> None:
    old = {"auto": {"trigger": "state"}}
    new = {"auto": {"trigger": "time", "at": "07:00"}}
    result = diff_structs(old, new)
    paths = {e.path for e in result.entries}
    assert "auto.trigger" in paths
    assert "auto.at" in paths


def test_diff_yaml_treats_missing_old_as_all_added() -> None:
    result = diff_yaml(None, "x: 1\ny: 2\n")
    assert result.summary["added"] == 2
    assert result.summary["removed"] == 0
    assert result.summary["changed"] == 0


def test_diff_yaml_ignores_whitespace_noise() -> None:
    # Same content, different formatting — should produce no entries.
    a = "x: 1\ny:\n  - a\n  - b\n"
    b = "x: 1\ny: [a, b]\n"
    result = diff_yaml(a, b)
    assert result.entries == []
