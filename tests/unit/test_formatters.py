"""Tests for tools.formatters — value translators + entity shaping."""

from __future__ import annotations

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries
from mylo.tools.formatters import (
    brightness_to_percent,
    color_temp_label,
    shape_entity,
    summarize_entities,
)


def test_brightness_to_percent() -> None:
    assert brightness_to_percent(0) == "0%"
    assert brightness_to_percent(255) == "100%"
    assert brightness_to_percent(128) == "50%"
    assert brightness_to_percent(-10) == "0%"
    assert brightness_to_percent(999) == "100%"
    assert brightness_to_percent(None) is None
    assert brightness_to_percent("nope") is None


def test_color_temp_label() -> None:
    assert color_temp_label(150) == "cool white"
    assert color_temp_label(250) == "neutral white"
    assert color_temp_label(400) == "warm white"
    assert color_temp_label(None) is None


def _registries() -> Registries:
    reg = Registries()
    reg.areas = {"kitchen": AreaEntry.from_raw({"area_id": "kitchen", "name": "Kitchen"})}
    reg.devices = {
        "d1": DeviceEntry.from_raw(
            {
                "id": "d1",
                "area_id": "kitchen",
                "manufacturer": "Philips",
                "model": "Hue White",
            }
        )
    }
    reg.entities = {
        "light.kitchen_overhead": EntityEntry.from_raw(
            {
                "entity_id": "light.kitchen_overhead",
                "original_name": "Kitchen Overhead",
                "platform": "hue",
                "device_id": "d1",
                "area_id": None,
                "labels": [],
            }
        ),
    }
    return reg


def test_shape_entity_translates_attributes_and_falls_back_to_device_area() -> None:
    reg = _registries()
    entry = reg.entities["light.kitchen_overhead"]
    state = {
        "entity_id": "light.kitchen_overhead",
        "state": "on",
        "attributes": {"brightness": 255, "color_temp": 370, "supported_features": 41},
    }
    shaped = shape_entity(entry, state, reg, include_attributes=True)
    assert shaped["friendly_name"] == "Kitchen Overhead"
    assert shaped["domain"] == "light"
    assert shaped["state"] == "on"
    assert shaped["area"] == "Kitchen"  # inherited from device
    assert shaped["device"] == "Philips Hue White"
    assert shaped["integration"] == "hue"
    assert shaped["key_attributes"]["brightness"] == "100%"
    assert shaped["key_attributes"]["color_temp"] == "warm white"
    # supported_features shouldn't bleed into key_attributes for lights.
    assert "supported_features" not in shaped["key_attributes"]


def test_shape_entity_without_state() -> None:
    reg = _registries()
    entry = reg.entities["light.kitchen_overhead"]
    shaped = shape_entity(entry, None, reg, include_attributes=True)
    assert shaped["state"] is None
    assert shaped["key_attributes"] == {}


def test_summarize_entities_counts_domains_and_on_states() -> None:
    envelope = summarize_entities(
        [
            {"domain": "light", "state": "on"},
            {"domain": "light", "state": "off"},
            {"domain": "sensor", "state": "23.4"},
        ],
        area_name="Kitchen",
    )
    assert envelope["entities_found"] == 3
    assert envelope["area"] == "Kitchen"
    # light comes before sensor because count=2>1; "1 on" annotated.
    assert envelope["summary"].startswith("2 lights (1 on)")
    assert "1 sensor" in envelope["summary"]
