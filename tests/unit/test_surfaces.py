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

from mylo.context.surfaces import TopologySurface
from mylo.context.tokens import estimate_tokens
from tests.unit.test_context_assembler import _fake_registries


def test_topology_surface_fits_budget() -> None:
    reg = _fake_registries()
    surface = TopologySurface(reg, memory=None)
    rendered = surface.render(50)
    assert rendered.tokens <= 50
    assert estimate_tokens(rendered.text) <= 50


def test_topology_surface_full_when_budget_ample() -> None:
    reg = _fake_registries()
    surface = TopologySurface(reg, memory=None)
    rendered = surface.render(100_000)
    assert "HOME TOPOLOGY:" in rendered.text


def test_topology_surface_sheds_areas_under_pressure() -> None:
    # A tiny-but-nonzero budget should drop areas yet stay within budget.
    reg = _fake_registries()
    surface = TopologySurface(reg, memory=None)
    rendered = surface.render(15)
    assert rendered.tokens <= 15
