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

"""Context budget authority: the Surface contract + priority allocator.

Every piece of prompt context is a Surface that renders within a token
budget. ``ContextBudget`` renders surfaces in priority order, lending any
unused budget downstream, and guarantees the assembled text fits the
total — so prompt size is a function of the budget, not of home size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mylo.context.tokens import context_window_for, estimate_tokens

_JOINER = "\n\n---\n\n"


@dataclass(slots=True, frozen=True)
class Rendered:
    """A surface's output. Invariant: ``tokens`` <= the render budget."""

    text: str
    tokens: int


@runtime_checkable
class Surface(Protocol):
    name: str

    def render(self, budget_tokens: int) -> Rendered: ...


@dataclass(slots=True, frozen=True)
class AssembledContext:
    text: str
    tokens_used: int
    allocations: list[tuple[str, int]]
    trimmed: bool


class TextSurface:
    """Atomic surface: emit the full text if it fits, else nothing.

    Used for fixed blocks (identity, hints, cost notes) that can't be
    partially rendered — they either make the budget or get dropped.
    """

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self._text = text
        self._tokens = estimate_tokens(text)

    def render(self, budget_tokens: int) -> Rendered:
        if not self._text or self._tokens > budget_tokens:
            return Rendered(text="", tokens=0)
        return Rendered(text=self._text, tokens=self._tokens)


class ContextBudget:
    """Renders surfaces in priority order within a fixed total."""

    def __init__(self, total_tokens: int) -> None:
        self.total_tokens = max(0, total_tokens)

    @classmethod
    def for_model(cls, model: str, *, factor: float, output_reserve: int) -> ContextBudget:
        window = context_window_for(model)
        total = int(window * factor) - output_reserve
        return cls(total_tokens=max(0, total))

    def render(self, surfaces: list[Surface]) -> AssembledContext:
        remaining = self.total_tokens
        parts: list[str] = []
        allocations: list[tuple[str, int]] = []
        trimmed = False
        for surface in surfaces:
            rendered = surface.render(remaining)
            if rendered.tokens > remaining:
                rendered = Rendered(text="", tokens=0)
            allocations.append((surface.name, rendered.tokens))
            if rendered.text:
                parts.append(rendered.text)
                remaining -= rendered.tokens
            else:
                trimmed = True
        return AssembledContext(
            text=_JOINER.join(parts),
            tokens_used=self.total_tokens - remaining,
            allocations=allocations,
            trimmed=trimmed,
        )
