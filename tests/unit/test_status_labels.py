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

"""Every registered tool has a plain-language status label."""

from __future__ import annotations

from mylo.llm.status_labels import FALLBACK_LABEL, label_for
from mylo.tools import registry


def test_every_registered_tool_has_an_explicit_label() -> None:
    registry._reset_for_tests()
    registry.load_all()
    try:
        missing = [name for name in registry.names() if label_for(name) == FALLBACK_LABEL]
    finally:
        registry._reset_for_tests()
    assert missing == []


def test_unknown_tool_falls_back() -> None:
    assert label_for("not_a_tool") == FALLBACK_LABEL


def test_labels_are_short_plain_phrases() -> None:
    assert label_for("query_automations") == "Reading your automations"
    assert label_for("apply_dashboard_plan") == "Updating the dashboard"
