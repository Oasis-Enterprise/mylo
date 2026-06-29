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

from __future__ import annotations

from mylo.tools.base import bound_rows


def test_under_budget_returns_all_rows() -> None:
    rows = [{"id": i} for i in range(5)]
    env = bound_rows(rows, max_rows=10)
    assert env["rows"] == rows
    assert env["total"] == 5
    assert "truncated" not in env


def test_over_budget_keeps_head_and_flags_truncation() -> None:
    rows = [{"id": i} for i in range(500)]
    env = bound_rows(rows, max_rows=100)
    assert env["total"] == 500
    assert env["truncated"] is True
    assert len(env["rows"]) == 100
    assert env["rows"][0] == {"id": 0}  # head, not tail
    assert "hint" in env


def test_custom_hint() -> None:
    env = bound_rows([{"x": i} for i in range(3)], max_rows=1, hint="use a filter")
    assert env["hint"] == "use a filter"
