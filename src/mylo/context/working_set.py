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

"""Relevance-ranked working set — the bounded entity surface.

Pre-loads the entities most likely relevant to the user's turn so the
model usually doesn't need a query round-trip, capped to a token budget
and a hard entity count. Purely additive: anything outside the set is
still reachable via the query tools, so completeness is never lost.
"""

from __future__ import annotations

from collections.abc import Mapping

from mylo.context.budget import Rendered
from mylo.context.tokens import estimate_tokens
from mylo.ha.registries import EntityEntry, Registries

_HEADER = "RELEVANT ENTITIES (pre-loaded for this turn; query for anything else):"


def _score(entity: EntityEntry, *, text_lc: str, monitored: set[str]) -> int:
    score = 0
    if entity.entity_id in text_lc:
        score += 100
    if entity.friendly_name and entity.friendly_name.lower() in text_lc:
        score += 80
    # Domain mention, e.g. "lights" / "light".
    if entity.domain in text_lc or f"{entity.domain}s" in text_lc:
        score += 20
    if entity.entity_id in monitored:
        score += 10
    return score


def rank_entities(
    registries: Registries,
    *,
    conversation_text: str,
    monitored: set[str],
    max_entities: int,
) -> list[EntityEntry]:
    text_lc = conversation_text.lower()
    scored: list[tuple[int, str, EntityEntry]] = []
    for entity in registries.entities.values():
        if entity.disabled_by or entity.hidden_by:
            continue
        s = _score(entity, text_lc=text_lc, monitored=monitored)
        if s > 0:
            scored.append((s, entity.entity_id, entity))
    # Sort by score desc, then entity_id for deterministic ties.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [e for _, _, e in scored[:max_entities]]


def _render_row(entity: EntityEntry, states: Mapping[str, str] | None) -> str:
    state = states.get(entity.entity_id) if states else None
    state_part = f" = {state}" if state is not None else ""
    return f"- {entity.entity_id} ({entity.friendly_name}){state_part}"


class WorkingSetSurface:
    name = "working_set"

    def __init__(
        self,
        registries: Registries,
        *,
        conversation_text: str,
        monitored: set[str],
        states: Mapping[str, str] | None,
        max_entities: int,
    ) -> None:
        self._registries = registries
        self._text = conversation_text
        self._monitored = monitored
        self._states = states
        self._max = max_entities

    def render(self, budget_tokens: int) -> Rendered:
        ranked = rank_entities(
            self._registries,
            conversation_text=self._text,
            monitored=self._monitored,
            max_entities=self._max,
        )
        if not ranked:
            return Rendered(text="", tokens=0)
        lines = [_HEADER]
        for entity in ranked:
            candidate = "\n".join([*lines, _render_row(entity, self._states)])
            if estimate_tokens(candidate) > budget_tokens:
                break
            lines.append(_render_row(entity, self._states))
        if len(lines) == 1:  # only the header would fit -> emit nothing
            return Rendered(text="", tokens=0)
        text = "\n".join(lines)
        return Rendered(text=text, tokens=estimate_tokens(text))
