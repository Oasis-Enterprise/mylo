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

"""Tests for the in-memory registry cache index helpers.

The network/refresh path is exercised via the probe script against a live HA;
here we only test the pure data-structure helpers.
"""

from __future__ import annotations

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries


def _make() -> Registries:
    reg = Registries()
    reg.entities = {
        "light.kitchen_overhead": EntityEntry.from_raw(
            {
                "entity_id": "light.kitchen_overhead",
                "name": None,
                "original_name": "Kitchen Overhead",
                "platform": "hue",
                "device_id": "dev1",
                "area_id": "kitchen",
                "labels": [],
            }
        ),
        "sensor.kitchen_temp": EntityEntry.from_raw(
            {
                "entity_id": "sensor.kitchen_temp",
                "original_name": "Kitchen Temp",
                "device_id": "dev2",
                "area_id": None,  # inherits from device below
                "labels": [],
            }
        ),
        "light.orphan": EntityEntry.from_raw(
            {
                "entity_id": "light.orphan",
                "original_name": "Orphan",
                "device_id": None,
                "area_id": None,
                "labels": [],
            }
        ),
        "switch.kitchen_fan": EntityEntry.from_raw(
            {
                "entity_id": "switch.kitchen_fan",
                "original_name": "Fan",
                "area_id": "kitchen",
                "labels": [],
            }
        ),
    }
    reg.devices = {
        "dev1": DeviceEntry.from_raw({"id": "dev1", "area_id": "kitchen"}),
        "dev2": DeviceEntry.from_raw({"id": "dev2", "area_id": "kitchen"}),
    }
    reg.areas = {
        "kitchen": AreaEntry.from_raw({"area_id": "kitchen", "name": "Kitchen"}),
    }
    return reg


def test_friendly_name_falls_back() -> None:
    e = EntityEntry.from_raw({"entity_id": "light.x", "original_name": "X"})
    assert e.friendly_name == "X"
    e2 = EntityEntry.from_raw({"entity_id": "light.y", "name": "Custom", "original_name": "Y"})
    assert e2.friendly_name == "Custom"


def test_domain_parses_from_entity_id() -> None:
    e = EntityEntry.from_raw({"entity_id": "binary_sensor.door"})
    assert e.domain == "binary_sensor"


def test_entities_by_area_includes_direct_assignments() -> None:
    reg = _make()
    ids = {e.entity_id for e in reg.entities_by_area("kitchen")}
    assert ids == {"light.kitchen_overhead", "switch.kitchen_fan"}


def test_entities_by_domain() -> None:
    reg = _make()
    assert {e.entity_id for e in reg.entities_by_domain("light")} == {
        "light.kitchen_overhead",
        "light.orphan",
    }


def test_domain_counts() -> None:
    reg = _make()
    assert reg.domain_counts() == {"light": 2, "sensor": 1, "switch": 1}


def test_unassigned_excludes_entities_whose_device_has_an_area() -> None:
    reg = _make()
    unassigned = {e.entity_id for e in reg.unassigned_entities()}
    # sensor.kitchen_temp has no area_id itself, but dev2 is in kitchen,
    # so it inherits and should NOT be unassigned. light.orphan has no
    # device at all → unassigned.
    assert unassigned == {"light.orphan"}
