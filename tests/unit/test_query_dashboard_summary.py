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

"""``_view_summary`` — section headings and per-card fingerprints.

Sections views list each section's heading and each card's
{index, type, entity?, heading?} so plan_dashboard ops can address a
section by heading and a card by index without a second query.
"""

from __future__ import annotations

from mylo.tools.read.query_dashboard import _view_summary


def test_sections_view_lists_headings_and_card_fingerprints() -> None:
    view = {
        "path": "rooms",
        "title": "Rooms",
        "type": "sections",
        "sections": [
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Lights"},
                    {"type": "tile", "entity": "light.a"},
                    {"type": "entities", "entities": [{"entity": "sensor.t"}]},
                ],
            },
            {"type": "grid", "cards": [{"type": "markdown", "content": "x"}]},
        ],
    }
    s = _view_summary(view)
    assert s["layout"] == "sections"
    assert s["section_count"] == 2
    assert s["sections"][0]["heading"] == "Lights"
    assert s["sections"][1]["heading"] is None
    assert s["sections"][0]["cards"] == [
        {"index": 0, "type": "heading", "heading": "Lights"},
        {"index": 1, "type": "tile", "entity": "light.a"},
        {"index": 2, "type": "entities", "entity": "sensor.t"},
    ]


def test_masonry_view_lists_cards() -> None:
    s = _view_summary({"path": "home", "cards": [{"type": "tile", "entity": "light.a"}]})
    assert "layout" not in s
    assert s["cards"] == [{"index": 0, "type": "tile", "entity": "light.a"}]
