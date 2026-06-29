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

from mylo.context.budget import ContextBudget, Rendered, TextSurface


class _FixedSurface:
    def __init__(self, name: str, text: str, tokens: int) -> None:
        self.name = name
        self._text = text
        self._tokens = tokens

    def render(self, budget_tokens: int) -> Rendered:
        if self._tokens > budget_tokens:
            return Rendered(text="", tokens=0)
        return Rendered(text=self._text, tokens=self._tokens)


def test_renders_in_order_and_joins() -> None:
    budget = ContextBudget(total_tokens=100)
    result = budget.render([_FixedSurface("a", "AAA", 10), _FixedSurface("b", "BBB", 10)])
    assert result.text == "AAA\n\n---\n\nBBB"
    assert result.tokens_used == 20
    assert result.allocations == [("a", 10), ("b", 10)]
    assert result.trimmed is False


def test_high_priority_starves_low_priority_when_over_budget() -> None:
    budget = ContextBudget(total_tokens=15)
    result = budget.render([_FixedSurface("big", "X", 10), _FixedSurface("nope", "Y", 10)])
    assert result.text == "X"
    assert result.trimmed is True
    assert ("nope", 0) in result.allocations


def test_empty_surfaces_are_skipped() -> None:
    budget = ContextBudget(total_tokens=100)
    result = budget.render([_FixedSurface("empty", "", 0), _FixedSurface("a", "AAA", 10)])
    assert result.text == "AAA"


def test_for_model_derives_total_from_window() -> None:
    budget = ContextBudget.for_model("claude-haiku-4-5", factor=0.6, output_reserve=8000)
    assert budget.total_tokens == 112_000


def test_text_surface_drops_when_too_big() -> None:
    s = TextSurface("note", "a" * 100)
    assert s.render(5).text == ""
    assert s.render(1000).text == "a" * 100
