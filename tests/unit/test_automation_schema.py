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

"""Tests for automation schema validation."""

from __future__ import annotations

from mylo.validators.automation_schema import (
    SchemaIssue,
    validate_automation,
    validate_script,
)


def _paths(issues: list[SchemaIssue]) -> list[str]:
    return [i.path for i in issues]


def test_valid_automation_passes() -> None:
    cfg = {
        "alias": "Morning",
        "trigger": [{"platform": "time", "at": "07:00"}],
        "condition": [{"condition": "state", "entity_id": "person.me", "state": "home"}],
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.x"}}],
        "mode": "single",
    }
    report = validate_automation(cfg)
    assert report.ok
    assert report.issues == []


def test_missing_trigger_and_action() -> None:
    report = validate_automation({"alias": "x"})
    paths = _paths(report.issues)
    assert "trigger" in paths
    assert "action" in paths


def test_unknown_platform_is_warning_not_error() -> None:
    cfg = {
        "trigger": [{"platform": "brand_new_platform", "topic": "x"}],
        "action": [{"service": "light.turn_on"}],
    }
    report = validate_automation(cfg)
    assert report.ok  # warnings don't fail
    assert any(i.severity == "warning" for i in report.issues)


def test_invalid_mode_errors() -> None:
    cfg = {
        "trigger": [{"platform": "time", "at": "07:00"}],
        "action": [{"service": "light.turn_on"}],
        "mode": "nope",
    }
    report = validate_automation(cfg)
    assert not report.ok
    assert any(i.path == "mode" for i in report.issues)


def test_service_must_be_domain_dot_name() -> None:
    cfg = {
        "trigger": [{"platform": "time", "at": "07:00"}],
        "action": [{"service": "turn_on"}],
    }
    report = validate_automation(cfg)
    assert not report.ok
    assert any("service" in i.path for i in report.issues)


def test_nested_choose_validates_inner_actions() -> None:
    cfg = {
        "trigger": [{"platform": "time", "at": "07:00"}],
        "action": [
            {
                "choose": [
                    {
                        "conditions": [{"condition": "state", "entity_id": "x.y", "state": "on"}],
                        "sequence": [{"service": "bogus"}],
                    }
                ],
                "default": [{"service": "light.turn_on"}],
            }
        ],
    }
    report = validate_automation(cfg)
    assert not report.ok
    assert any("choose[0].sequence" in i.path for i in report.issues)


def test_if_then_else() -> None:
    cfg = {
        "trigger": [{"platform": "time", "at": "07:00"}],
        "action": [
            {
                "if": [{"condition": "state", "entity_id": "x.y", "state": "on"}],
                "then": [{"service": "light.turn_on"}],
                "else": [{"service": "light.turn_off"}],
            }
        ],
    }
    report = validate_automation(cfg)
    assert report.ok


def test_list_of_automations() -> None:
    cfg = [
        {"trigger": [{"platform": "time"}], "action": [{"service": "a.b"}]},
        {"trigger": [{"platform": "time"}]},  # missing action
    ]
    report = validate_automation(cfg)
    assert not report.ok
    assert any("[1]" in i.path for i in report.issues)


def test_script_validation() -> None:
    assert validate_script({"sequence": [{"service": "a.b"}]}).ok
    assert not validate_script({}).ok
    assert not validate_script({"sequence": "not a list"}).ok
