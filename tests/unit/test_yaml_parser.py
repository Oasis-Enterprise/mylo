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


# ─── load_yaml_lenient ──────────────────────────────────────────────────────


def test_lenient_allows_duplicate_keys_first_wins() -> None:
    from mylo.validators.yaml_parser import load_yaml_lenient

    parsed = load_yaml_lenient("a: 1\nrejected: [1]\nrejected: [2]\n")
    assert parsed == {"a": 1, "rejected": [1]}


def test_strict_load_still_rejects_duplicate_keys() -> None:
    import pytest as _pytest
    from ruamel.yaml.constructor import DuplicateKeyError

    from mylo.validators.yaml_parser import load_yaml

    with _pytest.raises(DuplicateKeyError):
        load_yaml("rejected: [1]\nrejected: [2]\n")


def test_lenient_preserves_ha_tags() -> None:
    from mylo.validators.yaml_parser import PreservedTag, load_yaml_lenient

    parsed = load_yaml_lenient("password: !secret my_password\n")
    assert isinstance(parsed["password"], PreservedTag)
    assert parsed["password"].tag == "!secret"


def test_lenient_empty_text_returns_none() -> None:
    from mylo.validators.yaml_parser import load_yaml_lenient

    assert load_yaml_lenient("") is None
