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

"""A failed nightly sync becomes a finding and a status field; success clears both."""

from __future__ import annotations

from datetime import UTC, datetime

from mylo.memory.schema import empty_memory
from mylo.monitor.sync_outcome import apply_sync_outcome

NOW = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)


def _finding_ids(mem):  # type: ignore[no-untyped-def]
    return [pa.id for pa in mem.pending_actions]


def test_failure_records_error_and_finding() -> None:
    mem = empty_memory()
    changed = apply_sync_outcome(
        mem, summary="reconciler returned malformed YAML", failed=True, now=NOW
    )
    assert changed
    assert mem.last_sync_error == "reconciler returned malformed YAML"
    assert mem.last_sync_attempt == "2026-09-22T03:00:00+00:00"
    assert _finding_ids(mem) == ["mylo_sync_failed"]
    pa = mem.pending_actions[0]
    assert pa.type == "sync_failed" and pa.title == "Memory sync failed"
    assert (
        pa.message
        == "reconciler returned malformed YAML. Your notes are kept and Mylo will retry tonight."
    )


def test_success_clears_error_and_finding() -> None:
    mem = empty_memory()
    apply_sync_outcome(mem, summary="boom", failed=True, now=NOW)
    changed = apply_sync_outcome(mem, summary="merged 3 notes", failed=False, now=NOW)
    assert changed
    assert mem.last_sync_error is None
    assert _finding_ids(mem) == []


def test_success_without_prior_failure_is_a_noop() -> None:
    mem = empty_memory()
    assert apply_sync_outcome(mem, summary="merged", failed=False, now=NOW) is False


def test_repeat_failure_refreshes_not_duplicates() -> None:
    mem = empty_memory()
    apply_sync_outcome(mem, summary="a", failed=True, now=NOW)
    apply_sync_outcome(mem, summary="b", failed=True, now=NOW)
    assert _finding_ids(mem) == ["mylo_sync_failed"]
    assert mem.last_sync_error == "b"
