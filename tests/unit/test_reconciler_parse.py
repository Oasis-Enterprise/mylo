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


# ─── Repair ladder: duplicate keys + validation retries ─────────────────────
#
# Haiku sometimes emits the same top-level key twice (`rejected: []` at both
# the top and the bottom of the doc). ruamel's round-trip loader hard-fails
# on duplicates and the old single-retry (_fix_unquoted_colons) couldn't
# touch structure — the merge was skipped nightly. The parse is now a
# ladder: strict → strict+colon-fix → lenient (duplicates allowed,
# first occurrence wins) → lenient+colon-fix.


def test_parses_duplicate_top_level_key() -> None:
    text = (
        "version: 2\nrejected:\n  - id: r1\n    suggestion: first wins\nnotes: []\nrejected: []\n"
    )
    mf = _parse_reconciler_output(text)
    assert mf.version == 2
    # ruamel's allow_duplicate_keys keeps the FIRST occurrence — pinned
    # here so it's a conscious choice, not an accident.
    assert len(mf.rejected) == 1
    assert mf.rejected[0].suggestion == "first wins"


def test_duplicate_key_and_unquoted_colon_together() -> None:
    text = (
        "version: 2\n"
        "notes:\n"
        "  - id: n1\n"
        "    content: Tire specs (cold): 36 psi\n"
        "notes:\n"
        "  - id: n2\n"
        "    content: dupe\n"
    )
    mf = _parse_reconciler_output(text)
    assert mf.version == 2
    assert mf.notes[0].id == "n1"


def test_unparseable_output_still_raises() -> None:
    from ruamel.yaml.error import YAMLError

    with pytest.raises(YAMLError):
        _parse_reconciler_output(": : not yaml at all : [")


def test_non_mapping_output_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _parse_reconciler_output("- just\n- a\n- list\n")
