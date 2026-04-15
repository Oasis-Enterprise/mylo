"""Tests for Jinja2 template check + entity-ref extraction."""

from __future__ import annotations

from mylo.validators.template_check import (
    check_template,
    scan_config_for_templates,
)


def test_valid_template_extracts_entity_refs() -> None:
    result = check_template("{{ states('sensor.temp') | float > 70 }}")
    assert result.ok
    assert result.entity_refs == ["sensor.temp"]


def test_multiple_refs_in_one_template() -> None:
    result = check_template(
        "{% if is_state('light.x', 'on') and state_attr('climate.y', 'temperature') > 70 %}hot{% endif %}"
    )
    assert result.ok
    assert sorted(result.entity_refs) == ["climate.y", "light.x"]


def test_syntax_error_reports_line() -> None:
    result = check_template("{{ states('x') ")  # unclosed
    assert not result.ok
    assert result.errors and "line 1" in result.errors[0]


def test_dynamic_entity_ref_not_extracted() -> None:
    # We can only statically extract literal string args.
    result = check_template("{{ states(entity_var) }}")
    assert result.ok
    assert result.entity_refs == []


def test_scan_config_finds_templates_in_strings() -> None:
    cfg = {
        "trigger": [{"platform": "template", "value_template": "{{ states('sensor.x') }}"}],
        "action": [
            {
                "service": "notify.me",
                "data": {"message": "Temperature is {{ states('sensor.t') }}"},
            }
        ],
        "static_value": "no templates here",
    }
    found = scan_config_for_templates(cfg)
    paths = [p for p, _ in found]
    assert "trigger[0].value_template" in paths
    assert any("action[0].data.message" in p for p in paths)
    assert all("static_value" not in p for p in paths)
