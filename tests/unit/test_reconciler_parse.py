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

"""Tests for reconciler output parsing — especially code-fence stripping.

Haiku is told not to wrap its YAML in markdown fences but occasionally
does anyway. A truncated or unclosed ``` ```yaml ``` fence used to slip
through the old whole-string regex (which required a matching close) and
break the YAML parser on the leading backtick. These tests pin the
tolerant, line-wise behavior.
"""

from __future__ import annotations

import pytest

from mylo.memory.reconciler import _parse_reconciler_output, _strip_code_fence

_YAML_BODY = "version: 2\nnotes:\n  - id: n1\n    content: hello world\n"


def test_parses_bare_yaml() -> None:
    mf = _parse_reconciler_output(_YAML_BODY)
    assert mf.version == 2
    assert len(mf.notes) == 1


def test_parses_fenced_yaml_with_closing_fence() -> None:
    fenced = f"```yaml\n{_YAML_BODY}```"
    mf = _parse_reconciler_output(fenced)
    assert mf.version == 2
    assert len(mf.notes) == 1


def test_parses_fenced_yaml_without_closing_fence() -> None:
    # The regression: model opened a ```yaml fence but never closed it.
    # The old regex required both fences and left the backtick in place,
    # raising ScannerError. The tolerant stripper drops the open fence.
    fenced = f"```yaml\n{_YAML_BODY}"
    mf = _parse_reconciler_output(fenced)
    assert mf.version == 2
    assert len(mf.notes) == 1


def test_parses_bare_fence_no_language() -> None:
    fenced = f"```\n{_YAML_BODY}```"
    mf = _parse_reconciler_output(fenced)
    assert mf.version == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("```yaml\nfoo: 1\n```", "foo: 1"),
        ("```yaml\nfoo: 1", "foo: 1"),  # no close
        ("```\nfoo: 1\n```", "foo: 1"),
        ("foo: 1", "foo: 1"),  # no fence — unchanged
        ("```json\nfoo: 1\n```", "foo: 1"),  # other language tag
    ],
)
def test_strip_code_fence(text: str, expected: str) -> None:
    assert _strip_code_fence(text) == expected


# ─── []-for-string coercion (preferences) ───────────────────────────────────
#
# Haiku normalizes `null` string fields to `[]` when re-emitting the memory
# YAML. The nightly sync failed for weeks on `preferences.dashboard.notes:
# []` — pydantic wants str | None. Coerce empty containers to None on the
# three preference models (mirrors Note._flatten_dict_scope).


def test_parses_empty_list_for_string_preference_fields() -> None:
    text = (
        "version: 2\n"
        "preferences:\n"
        "  dashboard:\n"
        "    card_style: mushroom\n"
        "    notes: []\n"
        "  alerts:\n"
        "    quiet_hours: []\n"
        "  naming:\n"
        "    convention: []\n"
    )
    mf = _parse_reconciler_output(text)
    assert mf.preferences.dashboard.notes is None
    assert mf.preferences.dashboard.card_style == "mushroom"
    assert mf.preferences.alerts.quiet_hours is None
    assert mf.preferences.naming.convention is None


def test_coercion_leaves_real_list_fields_alone() -> None:
    from mylo.memory.schema import AlertPreferences, NamingPreferences

    alerts = AlertPreferences.model_validate({"quiet_hours": [], "channels": ["mobile"]})
    assert alerts.quiet_hours is None
    assert alerts.channels == ["mobile"]

    naming = NamingPreferences.model_validate(
        {"convention": {}, "examples": [{"before": "a", "after": "b"}]}
    )
    assert naming.convention is None
    assert naming.examples == [{"before": "a", "after": "b"}]


def test_coercion_keeps_valid_strings() -> None:
    from mylo.memory.schema import DashboardPreferences

    prefs = DashboardPreferences.model_validate({"notes": "keep it minimal", "theme": []})
    assert prefs.notes == "keep it minimal"
    assert prefs.theme is None
