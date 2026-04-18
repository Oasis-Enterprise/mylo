"""Tests for dashboard entity reference extraction and validation."""

from __future__ import annotations

from mylo.ha.registries import EntityEntry, Registries
from mylo.tools.dashboard_refs import extract_entity_refs, validate_refs


def _registries_with(*entity_ids: str) -> Registries:
    r = Registries()
    for eid in entity_ids:
        r.entities[eid] = EntityEntry(
            entity_id=eid,
            name=None,
            original_name=None,
            platform=None,
            device_id=None,
            area_id=None,
            labels=(),
            disabled_by=None,
            hidden_by=None,
        )
    return r


def test_extracts_entity_key() -> None:
    card = {"type": "entities", "entity": "light.kitchen"}
    refs = extract_entity_refs(card)
    assert "light.kitchen" in refs


def test_extracts_entity_id_list() -> None:
    card = {
        "type": "light",
        "entity_id": ["light.a", "light.b"],
    }
    refs = extract_entity_refs(card)
    assert refs == {"light.a", "light.b"}


def test_extracts_jinja_states() -> None:
    card = {
        "type": "custom:mushroom-title-card",
        "subtitle": "{% set t = states('sensor.basement_temp') %}{{ t }}° · {{ states('binary_sensor.motion') }}",
    }
    refs = extract_entity_refs(card)
    assert "sensor.basement_temp" in refs
    assert "binary_sensor.motion" in refs


def test_extracts_nested_entities_list() -> None:
    card = {
        "type": "custom:mini-graph-card",
        "entities": [
            {"entity": "sensor.power"},
            {"entity": "sensor.voltage"},
        ],
    }
    refs = extract_entity_refs(card)
    assert "sensor.power" in refs
    assert "sensor.voltage" in refs


def test_extracts_from_list_of_cards() -> None:
    cards = [
        {"type": "light", "entity": "light.a"},
        {"type": "light", "entity": "light.b"},
    ]
    refs = extract_entity_refs(cards)
    assert refs == {"light.a", "light.b"}


def test_extracts_camera_entity() -> None:
    card = {"type": "picture-entity", "camera_entity": "camera.front"}
    refs = extract_entity_refs(card)
    assert "camera.front" in refs


def test_validate_refs_catches_invalid() -> None:
    reg = _registries_with("light.kitchen_overhead", "light.kitchen_pendant")
    invalid = validate_refs(
        {"light.kitchen_overhead", "light.kitchen_lights"},
        reg,
    )
    assert len(invalid) == 1
    assert invalid[0]["entity_id"] == "light.kitchen_lights"
    assert len(invalid[0]["did_you_mean"]) > 0


def test_validate_refs_all_valid() -> None:
    reg = _registries_with("light.kitchen", "sensor.temp")
    invalid = validate_refs({"light.kitchen", "sensor.temp"}, reg)
    assert invalid == []


def test_validate_refs_fuzzy_suggests_close_match() -> None:
    reg = _registries_with(
        "light.basement_celing",
        "light.basement_lamp_helper",
    )
    invalid = validate_refs({"light.basement_ceiling"}, reg)
    assert len(invalid) == 1
    suggestions = invalid[0]["did_you_mean"]
    assert "light.basement_celing" in suggestions
