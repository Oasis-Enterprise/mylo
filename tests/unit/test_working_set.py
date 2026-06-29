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

from mylo.context.working_set import WorkingSetSurface, rank_entities
from tests.unit.test_context_assembler import _fake_registries


def test_mentioned_entity_ranks_first() -> None:
    reg = _fake_registries()
    any_id = next(iter(reg.entities))
    ranked = rank_entities(
        reg,
        conversation_text=f"what is {any_id} doing",
        monitored=set(),
        max_entities=5,
    )
    assert ranked[0].entity_id == any_id


def test_domain_mention_ranks_matching_domain() -> None:
    reg = _fake_registries()
    ranked = rank_entities(
        reg, conversation_text="turn off the lights", monitored=set(), max_entities=5
    )
    assert ranked
    assert any(e.domain == "light" for e in ranked)


def test_working_set_surface_respects_budget_and_cap() -> None:
    reg = _fake_registries()
    surface = WorkingSetSurface(
        reg, conversation_text="lights", monitored=set(), states=None, max_entities=3
    )
    rendered = surface.render(10_000)
    assert rendered.text.count("\n") <= 3 + 1  # header + at most 3 rows
    tiny = surface.render(1)
    assert tiny.tokens <= 1


def test_no_relevant_entities_renders_nothing() -> None:
    reg = _fake_registries()
    surface = WorkingSetSurface(
        reg,
        conversation_text="zzz nothing matches here",
        monitored=set(),
        states=None,
        max_entities=5,
    )
    assert surface.render(10_000).text == ""
