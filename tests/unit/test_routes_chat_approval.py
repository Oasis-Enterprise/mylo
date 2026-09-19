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

"""The chat route threads approved_plan_ids into the per-turn ToolContext."""

from __future__ import annotations

from mylo.server.routes_chat import _approved_plan_ids_from_body


def test_parses_string_list() -> None:
    assert _approved_plan_ids_from_body({"approved_plan_ids": ["a", "b"]}) == frozenset({"a", "b"})


def test_ignores_garbage() -> None:
    assert _approved_plan_ids_from_body({}) == frozenset()
    assert _approved_plan_ids_from_body({"approved_plan_ids": "a"}) == frozenset()
    assert _approved_plan_ids_from_body({"approved_plan_ids": ["a", 3, None]}) == frozenset({"a"})
