"""Tests for YAML 1.1 ambiguity defense in dump_yaml.

HA's PyYAML parser treats unquoted ``23:00:00`` as a sexagesimal
integer (82800), not a time string. dump_yaml must quote ambiguous
values before emitting them.
"""

from __future__ import annotations

import pytest

from mylo.validators.yaml_parser import dump_yaml, load_yaml


@pytest.mark.parametrize(
    "value",
    [
        "23:00:00",
        "07:00",
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "null",
        "~",
        "42",
        "3.14",
    ],
)
def test_ambiguous_strings_are_quoted(value: str) -> None:
    out = dump_yaml({"at": value})
    # Must appear with quotes around the value.
    assert f'"{value}"' in out, f"expected quoted {value!r} in:\n{out}"


def test_round_trip_preserves_string_identity() -> None:
    # Dump → Load should recover the original Python string, not a
    # coerced type, even for ambiguous values.
    source = {"at": "23:00:00", "mode": "single", "count": 5}
    dumped = dump_yaml(source)
    parsed = load_yaml(dumped)
    assert parsed["at"] == "23:00:00"
    assert isinstance(parsed["at"], str)
    assert parsed["mode"] == "single"
    assert parsed["count"] == 5


def test_normal_strings_are_not_quoted() -> None:
    # Aliases and regular prose should stay readable.
    out = dump_yaml({"alias": "Basement Ceiling Off at 11PM"})
    assert "alias: Basement Ceiling Off at 11PM" in out
    assert '"Basement' not in out


def test_automation_at_value_round_trip() -> None:
    automation = {
        "automation": [
            {
                "id": "mylo_test",
                "alias": "Test",
                "trigger": [{"platform": "time", "at": "23:00:00"}],
                "action": [{"service": "light.turn_off"}],
            }
        ]
    }
    dumped = dump_yaml(automation)
    # The at value must be quoted in the output.
    assert '"23:00:00"' in dumped
    # And round-tripping keeps it a string.
    reloaded = load_yaml(dumped)
    assert reloaded["automation"][0]["trigger"][0]["at"] == "23:00:00"
