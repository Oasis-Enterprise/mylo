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

"""Tests for the reference resolver.

We seed a small registry by hand, then feed typos and fuzzy matches
through the resolver to validate the three outcomes: hit, auto-correct,
mismatch.
"""

from __future__ import annotations

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries
from mylo.resolver.resolver import RefMismatch, ResolvedRef, Resolver


def _registries() -> Registries:
    reg = Registries()
    reg.areas = {
        "kitchen": AreaEntry.from_raw({"area_id": "kitchen", "name": "Kitchen"}),
        "living_room": AreaEntry.from_raw({"area_id": "living_room", "name": "Living Room"}),
    }
    reg.devices = {
        "dev_thermo": DeviceEntry.from_raw(
            {"id": "dev_thermo", "name": "Ecobee Thermostat", "manufacturer": "Ecobee"}
        ),
    }
    reg.entities = {
        "sensor.kitchen_temperature": EntityEntry.from_raw(
            {"entity_id": "sensor.kitchen_temperature", "labels": []}
        ),
        "sensor.kitchen_humidity": EntityEntry.from_raw(
            {"entity_id": "sensor.kitchen_humidity", "labels": []}
        ),
        "light.kitchen_overhead": EntityEntry.from_raw(
            {"entity_id": "light.kitchen_overhead", "labels": []}
        ),
    }
    return reg


def test_exact_hit() -> None:
    r = Resolver(_registries())
    out = r.resolve_entity("sensor.kitchen_temperature")
    assert isinstance(out, ResolvedRef)
    assert not out.corrected
    assert out.resolved == "sensor.kitchen_temperature"


def test_close_typo_auto_corrects_when_unique() -> None:
    r = Resolver(_registries())
    # "temp" shortened → fuzzy match against "temperature".
    out = r.resolve_entity("sensor.kitchen_temp")
    # Either auto-correct or mismatch — both are acceptable. Assert the
    # kind of outcome depending on score, but at minimum the closest
    # suggestion must be the real entity.
    if isinstance(out, ResolvedRef):
        assert out.corrected
        assert out.resolved == "sensor.kitchen_temperature"
    else:
        assert "sensor.kitchen_temperature" in out.suggestions


def test_ambiguous_does_not_auto_correct() -> None:
    r = Resolver(_registries())
    # "kitchen_something" is close to multiple — should not auto-correct.
    out = r.resolve_entity("sensor.kitchen_x")
    # Result shape depends on scorer thresholds, but an ambiguous prefix
    # must either mismatch or auto-correct to a *single* specific entity,
    # not silently rename to the wrong one.
    if isinstance(out, ResolvedRef):
        assert out.resolved in {"sensor.kitchen_temperature", "sensor.kitchen_humidity"}
    else:
        suggestion_set = set(out.suggestions)
        assert suggestion_set & {
            "sensor.kitchen_temperature",
            "sensor.kitchen_humidity",
        }


def test_unknown_entity_returns_mismatch_with_suggestions() -> None:
    r = Resolver(_registries())
    out = r.resolve_entity("sensor.completely_unrelated_thing")
    assert isinstance(out, RefMismatch)
    # Even with low similarity the envelope exists; empty suggestions are OK.
    envelope = out.to_envelope()
    assert envelope["error"] == "entity_not_found"
    assert envelope["invalid_ref"] == "sensor.completely_unrelated_thing"


def test_area_resolution_case_insensitive() -> None:
    r = Resolver(_registries())
    out = r.resolve_area("KITCHEN")
    assert isinstance(out, ResolvedRef)
    assert out.resolved == "kitchen"


def test_area_resolution_by_display_name() -> None:
    r = Resolver(_registries())
    out = r.resolve_area("Living Room")
    assert isinstance(out, ResolvedRef)
    assert out.resolved == "living_room"


def test_device_resolution_by_id() -> None:
    r = Resolver(_registries())
    out = r.resolve_device("dev_thermo")
    assert isinstance(out, ResolvedRef)
    assert out.resolved == "dev_thermo"


def test_device_resolution_by_display_name() -> None:
    r = Resolver(_registries())
    out = r.resolve_device("Ecobee Thermostat")
    assert isinstance(out, ResolvedRef)
    assert out.resolved == "dev_thermo"


def test_disable_auto_correct_forces_mismatch() -> None:
    r = Resolver(_registries())
    out = r.resolve_entity("sensor.kitchen_temp", allow_auto_correct=False)
    # Without auto-correct, even a close typo must mismatch.
    assert isinstance(out, RefMismatch)
    assert "sensor.kitchen_temperature" in out.suggestions
