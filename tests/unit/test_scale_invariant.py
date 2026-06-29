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

"""The headline scale guarantee: the assembled prompt is always bounded,
and the two real-world failures (big reconciler payload, oversized prompt)
cannot recur."""

from __future__ import annotations

from pathlib import Path

import pytest

from mylo.context.assembler import assemble_system_prompt
from mylo.context.basic_prompt import LoadedPrompt
from mylo.context.tokens import estimate_tokens
from mylo.memory.reconciler import compact_payload_sections
from mylo.memory.schema import MemoryFile, Note
from tests.unit._scale_fixtures import make_big_registry


@pytest.mark.parametrize("size", [100, 1000, 2484, 5000, 10000])
def test_prompt_always_under_budget(size: int, tmp_path: Path) -> None:
    reg = make_big_registry(entities=size)
    result = assemble_system_prompt(
        registries=reg,
        memory=MemoryFile(),
        conversation_text="turn off the kitchen lights and check the climate",
        mylo_data_dir=tmp_path,
        base_prompt=LoadedPrompt(text="identity", version="v"),
        model="claude-sonnet-4-6",
        budget_factor=0.6,
        output_reserve=8000,
    )
    budget = int(1_000_000 * 0.6) - 8000
    assert estimate_tokens(result.system) <= budget


def test_prompt_bounded_under_a_tiny_budget(tmp_path: Path) -> None:
    # Even a small slice of a huge home must respect the budget exactly.
    reg = make_big_registry(entities=10000)
    result = assemble_system_prompt(
        registries=reg,
        memory=MemoryFile(),
        conversation_text="lights",
        mylo_data_dir=tmp_path,
        base_prompt=LoadedPrompt(text="identity", version="v"),
        model="claude-haiku-4-5",
        budget_factor=0.0015,  # 200_000 * 0.0015 = 300 tokens
        output_reserve=0,
    )
    assert estimate_tokens(result.system) <= 300


def test_regression_reconciler_payload_bounded() -> None:
    # The 214K-token failure: a bloated memory must compact to fit, not skip.
    mem = MemoryFile()
    mem.notes = [Note(id=f"n{i}", content="x" * 500) for i in range(5000)]
    compacted, marker, _ = compact_payload_sections(mem, budget_tokens=100_000)
    assert estimate_tokens(compacted.model_dump_json()) <= 100_000
    assert marker  # something was compacted
