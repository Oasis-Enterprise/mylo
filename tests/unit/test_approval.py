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

"""Preview-id fingerprints bind approval to one exact call."""

from __future__ import annotations

from mylo.safety.approval import preview_id


def test_stable_across_key_order_and_dry_run() -> None:
    a = preview_id(
        "modify_automation", {"action": "create", "config": {"alias": "x"}, "dry_run": True}
    )
    b = preview_id(
        "modify_automation", {"dry_run": False, "config": {"alias": "x"}, "action": "create"}
    )
    assert a == b
    assert a.startswith("pv_") and len(a) == 15


def test_changes_with_any_param_or_tool() -> None:
    base = preview_id("t", {"a": 1})
    assert preview_id("t", {"a": 2}) != base
    assert preview_id("u", {"a": 1}) != base
    assert preview_id("t", {"a": 1, "b": None}) != base
