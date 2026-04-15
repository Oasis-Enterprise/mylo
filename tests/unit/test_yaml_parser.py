"""Tests for YAML round-trip + HA magic tags."""

from __future__ import annotations

from mylo.validators.yaml_parser import (
    PreservedTag,
    dump_yaml,
    load_yaml,
    safe_load,
)


def test_round_trip_preserves_comments_and_key_order() -> None:
    source = """\
# top-level comment
alias: Morning Routine
trigger:
  - platform: time  # trigger comment
    at: "07:00"
action:
  - service: light.turn_on
"""
    parsed = load_yaml(source)
    assert parsed["alias"] == "Morning Routine"
    dumped = dump_yaml(parsed)
    assert "# top-level comment" in dumped
    assert "# trigger comment" in dumped
    # Key order preserved.
    assert dumped.index("alias") < dumped.index("trigger") < dumped.index("action")


def test_preserves_ha_secret_tag() -> None:
    source = "api_key: !secret anthropic_key\n"
    parsed = load_yaml(source)
    assert isinstance(parsed["api_key"], PreservedTag)
    assert parsed["api_key"].tag == "!secret"
    assert parsed["api_key"].value == "anthropic_key"
    dumped = dump_yaml(parsed)
    assert "!secret anthropic_key" in dumped


def test_preserves_include_tag() -> None:
    source = "automation: !include automations.yaml\n"
    parsed = load_yaml(source)
    assert isinstance(parsed["automation"], PreservedTag)
    assert parsed["automation"].tag == "!include"
    assert "!include automations.yaml" in dump_yaml(parsed)


def test_safe_load_returns_plain_types() -> None:
    parsed = safe_load("foo: 1\nbar:\n  - a\n  - b\n")
    assert parsed == {"foo": 1, "bar": ["a", "b"]}


def test_empty_text_loads_to_none() -> None:
    assert load_yaml("") is None
    assert safe_load("") is None


def test_dump_adds_trailing_newline() -> None:
    assert dump_yaml({"x": 1}).endswith("\n")
