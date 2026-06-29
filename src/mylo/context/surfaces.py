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

"""Concrete budget-aware surfaces wrapping the existing prompt builders.

The atomic blocks (identity, hints, proposals, cost notes) use
``TextSurface`` directly. The elastic blocks — topology and the working
set — get dedicated surfaces that shed their lowest-value content to fit
a shrinking budget instead of being all-or-nothing.
"""

from __future__ import annotations

from mylo.context.budget import Rendered
from mylo.context.tokens import estimate_tokens
from mylo.context.topology import build_topology, format_topology
from mylo.ha.registries import Registries
from mylo.memory.schema import MemoryFile


class TopologySurface:
    name = "topology"

    def __init__(self, registries: Registries, *, memory: MemoryFile | None) -> None:
        self._registries = registries
        self._memory = memory

    def render(self, budget_tokens: int) -> Rendered:
        topology = build_topology(self._registries, memory=self._memory)
        text = format_topology(topology)
        tokens = estimate_tokens(text)
        if tokens <= budget_tokens:
            return Rendered(text=text, tokens=tokens)
        # Too big: drop areas from the bottom (already sorted by entity
        # count, so the tail is least-populated) until it fits.
        areas = topology.get("areas") or {}
        area_items = list(areas.items())
        while area_items and tokens > budget_tokens:
            area_items.pop()
            topology["areas"] = dict(area_items)
            topology["areas_truncated"] = True
            text = format_topology(topology)
            tokens = estimate_tokens(text)
        if tokens > budget_tokens:
            return Rendered(text="", tokens=0)
        return Rendered(text=text, tokens=tokens)
