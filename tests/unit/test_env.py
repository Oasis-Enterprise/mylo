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

"""Tests for the .env loader."""

from __future__ import annotations

from pathlib import Path

from mylo.util.env import load_dotenv


def test_load_dotenv_missing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MYLO_TEST_UNSET", raising=False)
    loaded = load_dotenv(tmp_path / "does-not-exist.env")
    assert loaded == {}


def test_load_dotenv_parses_simple_pairs(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / ".env"
    f.write_text(
        """
        # a comment
        FOO=bar
        QUOTED_DOUBLE="hello world"
        QUOTED_SINGLE='nope'
        EMPTY=
        BAD LINE NO EQUALS
        WITH_EQ=a=b=c
        """.strip()
    )
    for key in ("FOO", "QUOTED_DOUBLE", "QUOTED_SINGLE", "EMPTY", "WITH_EQ"):
        monkeypatch.delenv(key, raising=False)

    loaded = load_dotenv(f)

    assert loaded["FOO"] == "bar"
    assert loaded["QUOTED_DOUBLE"] == "hello world"
    assert loaded["QUOTED_SINGLE"] == "nope"
    assert loaded["EMPTY"] == ""
    assert loaded["WITH_EQ"] == "a=b=c"
    assert "BAD LINE NO EQUALS" not in loaded


def test_load_dotenv_does_not_override_by_default(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / ".env"
    f.write_text("X=from_env_file")
    monkeypatch.setenv("X", "from_process")

    loaded = load_dotenv(f)

    assert "X" not in loaded
    import os

    assert os.environ["X"] == "from_process"


def test_load_dotenv_can_override(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / ".env"
    f.write_text("X=from_env_file")
    monkeypatch.setenv("X", "from_process")

    loaded = load_dotenv(f, override=True)

    assert loaded["X"] == "from_env_file"
    import os

    assert os.environ["X"] == "from_env_file"
