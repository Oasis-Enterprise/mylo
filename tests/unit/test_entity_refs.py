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

"""Tests for entity-ref validation against a config."""

from __future__ import annotations

from mylo.ha.registries import EntityEntry, Registries
from mylo.resolver.resolver import Resolver
from mylo.validators.entity_refs import check_entity_refs, extract_refs


def _registries(entities: list[str]) -> Registries:
    reg = Registries()
    reg.entities = {eid: EntityEntry.from_raw({"entity_id": eid, "labels": []}) for eid in entities}
    return reg


def test_extract_refs_finds_service_targets_and_templates() -> None:
    cfg = {
        "trigger": [{"platform": "state", "entity_id": "sensor.a"}],
        "condition": [
            {
                "condition": "template",
                "value_template": "{{ states('sensor.b') | float > 0 }}",
            }
        ],
        "action": [
            {
                "service": "light.turn_on",
                "target": {"entity_id": ["light.c", "light.d"]},
            }
        ],
    }
    refs = extract_refs(cfg)
    ids = sorted({r.ref for r in refs})
    assert ids == ["light.c", "light.d", "sensor.a", "sensor.b"]


def test_check_entity_refs_passes_when_all_exist() -> None:
    reg = _registries(["sensor.a", "light.c"])
    resolver = Resolver(reg)
    cfg = {
        "trigger": [{"platform": "state", "entity_id": "sensor.a"}],
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.c"}}],
    }
    result = check_entity_refs(cfg, resolver)
    assert result.ok
    assert not result.mismatches


def test_check_entity_refs_fails_on_missing() -> None:
    reg = _registries(["sensor.a", "light.c"])
    resolver = Resolver(reg)
    cfg = {
        "trigger": [{"platform": "state", "entity_id": "sensor.missing"}],
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.c"}}],
    }
    result = check_entity_refs(cfg, resolver)
    assert not result.ok
    paths = [p for p, _ in result.mismatches]
    assert any("trigger[0].entity_id" in p for p in paths)


def test_check_entity_refs_reports_broken_template() -> None:
    reg = _registries(["sensor.a"])
    resolver = Resolver(reg)
    cfg = {
        "trigger": [
            {
                "platform": "template",
                "value_template": "{{ states('sensor.a') ",  # unclosed
            }
        ],
        "action": [{"service": "a.b"}],
    }
    result = check_entity_refs(cfg, resolver)
    assert not result.ok
    assert result.template_errors
