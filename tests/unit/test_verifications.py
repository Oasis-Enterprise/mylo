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

"""VerificationLog: start → complete → acknowledge, persistence, capacity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mylo.files.verifications import VerificationLog, render_verifications

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _log(tmp_path: Path, clock: _Clock | None = None, **kw: int) -> VerificationLog:
    return VerificationLog(tmp_path / "verifications.json", clock=clock or _Clock(), **kw)


def test_start_records_pending_and_persists(tmp_path: Path) -> None:
    log = _log(tmp_path)
    v = log.start(tool="modify_automation", target="automation porch", conversation_id="c1")
    assert v.id.startswith("vf_") and len(v.id) == 15
    assert v.status == "pending" and v.completed_at is None and v.acknowledged is False
    reloaded = _log(tmp_path)
    assert reloaded.get(v.id) is not None
    assert reloaded.get(v.id).status == "pending"  # type: ignore[union-attr]


def test_complete_sets_status_and_time(tmp_path: Path) -> None:
    clock = _Clock()
    log = _log(tmp_path, clock)
    v = log.start(tool="t", target="x", conversation_id="c")
    clock.now = T0 + timedelta(seconds=45)
    done = log.complete(v.id, status="verified", message="entity present")
    assert done is not None and done.status == "verified"
    assert done.completed_at == "2026-09-22T12:00:45+00:00"
    assert log.unacknowledged() == [done]


def test_complete_rejects_bad_status_and_unknown_id(tmp_path: Path) -> None:
    log = _log(tmp_path)
    v = log.start(tool="t", target="x", conversation_id="c")
    with pytest.raises(ValueError):
        log.complete(v.id, status="pending", message="")
    assert log.complete("vf_nope", status="failed", message="m") is None


def test_acknowledge_hides_from_unacknowledged(tmp_path: Path) -> None:
    log = _log(tmp_path)
    v = log.start(tool="t", target="x", conversation_id="c")
    log.complete(v.id, status="failed", message="no")
    assert log.acknowledge(v.id) is True
    assert log.unacknowledged() == []
    assert log.acknowledge("vf_nope") is False
    assert _log(tmp_path).get(v.id).acknowledged is True  # type: ignore[union-attr]


def test_recent_filters_by_window_newest_first(tmp_path: Path) -> None:
    clock = _Clock()
    log = _log(tmp_path, clock)
    old = log.start(tool="t", target="old", conversation_id="c")
    clock.now = T0 + timedelta(hours=30)
    new = log.start(tool="t", target="new", conversation_id="c")
    assert [v.id for v in log.recent(hours=24)] == [new.id]
    assert [v.id for v in log.recent(hours=48)] == [new.id, old.id]


def test_capacity_keeps_newest(tmp_path: Path) -> None:
    log = _log(tmp_path, capacity=3)
    ids = [log.start(tool="t", target=str(i), conversation_id="c").id for i in range(5)]
    assert [v.id for v in _log(tmp_path, capacity=3).recent(hours=1)] == list(reversed(ids[2:]))


def test_corrupt_file_resets_to_empty(tmp_path: Path) -> None:
    (tmp_path / "verifications.json").write_text("{not json", encoding="utf-8")
    assert _log(tmp_path).recent() == []


def test_render_lists_outcomes(tmp_path: Path) -> None:
    log = _log(tmp_path)
    a = log.start(tool="modify_automation", target="automation porch", conversation_id="c")
    log.complete(a.id, status="failed", message="websocket never reconnected")
    b = log.start(tool="rename_entities", target="light.porch", conversation_id="c")
    text = render_verifications(log.recent())
    assert "VERIFICATION OUTCOMES" in text
    assert "12:00 modify_automation automation porch — FAILED: websocket never reconnected" in text
    assert "12:00 rename_entities light.porch — pending" in text
    assert b.id not in text
