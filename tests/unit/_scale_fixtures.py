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

"""Synthetic large-home registry builder for scale tests."""

from __future__ import annotations

from mylo.ha.registries import AreaEntry, EntityEntry, Registries

_DOMAINS = ["light", "switch", "sensor", "binary_sensor", "climate", "cover"]


def make_big_registry(*, entities: int, areas: int = 50) -> Registries:
    reg = Registries()
    reg.areas = {
        f"area_{a}": AreaEntry(area_id=f"area_{a}", name=f"Area {a}", floor_id=None, labels=())
        for a in range(areas)
    }
    ents: dict[str, EntityEntry] = {}
    for i in range(entities):
        dom = _DOMAINS[i % len(_DOMAINS)]
        eid = f"{dom}.entity_{i}"
        ents[eid] = EntityEntry(
            entity_id=eid,
            name=f"Entity {i}",
            original_name=None,
            platform="demo",
            device_id=None,
            area_id=f"area_{i % areas}",
            labels=(),
            disabled_by=None,
            hidden_by=None,
        )
    reg.entities = ents
    return reg
