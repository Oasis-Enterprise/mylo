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
from mylo.context.tokens import context_window_for, estimate_tokens


class _FixedSurface:
    """Fake surface emitting fixed text. Accounting now derives from the
    text itself (the budget re-estimates), so it self-trims on size."""

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self._text = text

    def render(self, budget_tokens: int) -> Rendered:
        tokens = estimate_tokens(self._text)
        if tokens > budget_tokens:
            return Rendered(text="", tokens=0)
        return Rendered(text=self._text, tokens=tokens)


def test_renders_in_order_and_joins() -> None:
    budget = ContextBudget(total_tokens=100)
    result = budget.render([_FixedSurface("a", "AAA"), _FixedSurface("b", "BBB")])
    assert result.text == "AAA\n\n---\n\nBBB"
    assert result.tokens_used == estimate_tokens("AAA") + estimate_tokens("BBB")
    assert result.allocations == [("a", estimate_tokens("AAA")), ("b", estimate_tokens("BBB"))]
    assert result.trimmed is False


def test_high_priority_starves_low_priority_when_over_budget() -> None:
    # Each surface needs 10 tokens; budget of 10 fits the first, starves the second.
    big = "a" * 35  # estimate_tokens -> ceil(35/3.5) = 10
    budget = ContextBudget(total_tokens=10)
    result = budget.render([_FixedSurface("big", big), _FixedSurface("nope", big)])
    assert result.text == big
    assert result.trimmed is True
    assert ("nope", 0) in result.allocations


def test_empty_surfaces_are_skipped() -> None:
    budget = ContextBudget(total_tokens=100)
    result = budget.render([_FixedSurface("empty", ""), _FixedSurface("a", "AAA")])
    assert result.text == "AAA"
    assert result.trimmed is True  # the empty surface emitted nothing


def test_for_model_derives_total_from_window() -> None:
    budget = ContextBudget.for_model("claude-haiku-4-5", factor=0.6, output_reserve=8000)
    assert budget.total_tokens == int(context_window_for("claude-haiku-4-5") * 0.6) - 8000


def test_text_surface_drops_when_too_big() -> None:
    s = TextSurface("note", "a" * 100)
    assert s.render(5).text == ""
    assert s.render(1000).text == "a" * 100
