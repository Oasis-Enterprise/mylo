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

from pathlib import Path
from typing import Any

from mylo.ha.registries import Registries
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from mylo.tools.read.query_dashboard import _view_summary
from tests.unit._helpers import make_ctx


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
    s = _view_summary(view, include_cards=True)
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
    s = _view_summary(
        {"path": "home", "cards": [{"type": "tile", "entity": "light.a"}]}, include_cards=True
    )
    assert "layout" not in s
    assert s["cards"] == [{"index": 0, "type": "tile", "entity": "light.a"}]


def test_listing_summary_has_counts_not_cards() -> None:
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
    s = _view_summary(view, include_cards=False)
    assert s["sections"][0] == {"index": 0, "heading": "Lights", "card_count": 3}
    assert "cards" not in s["sections"][0]

    masonry = _view_summary(
        {"path": "home", "cards": [{"type": "tile", "entity": "light.a"}]}, include_cards=False
    )
    assert masonry["card_count"] == 1
    assert "cards" not in masonry


class _FakeClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        if type_ == "lovelace/config":
            return self._config
        return {}


async def test_single_view_response_includes_indexed_sections(tmp_path: Path) -> None:
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    try:
        rooms_view = {
            "path": "rooms",
            "title": "Rooms",
            "type": "sections",
            "sections": [
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Lights"},
                        {"type": "tile", "entity": "light.a"},
                    ],
                }
            ],
        }
        config = {"title": "Main", "views": [rooms_view]}
        client = _FakeClient(config)
        ctx = make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path)
        result = await execute("query_dashboard", {"dashboard_id": "x", "view_id": "rooms"}, ctx)
        assert result.status.value == "ok", result.error_message
        assert result.data["view"] == rooms_view
        assert result.data["sections"][0]["cards"][1] == {
            "index": 1,
            "type": "tile",
            "entity": "light.a",
        }
    finally:
        tool_registry._reset_for_tests()
