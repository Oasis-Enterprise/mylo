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

"""Tests for the patch_config_file dotted-path mini-language."""

from __future__ import annotations

import pytest

from mylo.tools.write.patch_config_file import APPEND, PatchError, _apply_patch, _parse_path


def test_parse_basic_key_path() -> None:
    assert _parse_path("homeassistant.customize") == ["homeassistant", "customize"]


def test_parse_positive_index() -> None:
    assert _parse_path("automation[0].alias") == ["automation", 0, "alias"]


def test_parse_negative_index() -> None:
    assert _parse_path("automation[-1]") == ["automation", -1]


def test_parse_append_plus() -> None:
    parsed = _parse_path("automation[+]")
    assert parsed[:-1] == ["automation"]
    assert parsed[-1] is APPEND


def test_parse_append_dash() -> None:
    parsed = _parse_path("automation[-]")
    assert parsed[-1] is APPEND


def test_apply_append_adds_to_end() -> None:
    struct = {"automation": [{"alias": "first"}]}
    result = _apply_patch(struct, "automation[+]", "add", {"alias": "second"})
    assert result["automation"] == [{"alias": "first"}, {"alias": "second"}]


def test_apply_append_on_non_list_errors() -> None:
    struct = {"automation": {"alias": "first"}}
    with pytest.raises(PatchError) as exc:
        _apply_patch(struct, "automation[+]", "add", {"alias": "second"})
    assert exc.value.code == "path_type_mismatch"


def test_apply_negative_index_inserts_from_end() -> None:
    struct = {"items": ["a", "b", "c"]}
    # [-1] insert-before semantics: insert before the last element.
    _apply_patch(struct, "items[-1]", "add", "new")
    # "new" should end up as second-to-last.
    assert struct["items"] == ["a", "b", "new", "c"]


def test_apply_positive_index_insert_at_position() -> None:
    struct = {"items": ["a", "b", "c"]}
    _apply_patch(struct, "items[1]", "add", "new")
    assert struct["items"] == ["a", "new", "b", "c"]


def test_top_level_list_append() -> None:
    # Automations.yaml is a top-level list — support appending there.
    struct = [{"id": "one"}]
    _apply_patch(struct, "[+]", "add", {"id": "two"})
    assert struct == [{"id": "one"}, {"id": "two"}]
