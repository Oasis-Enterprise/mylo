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

"""Smoke tests for config loading."""

from __future__ import annotations

import json
from pathlib import Path

from mylo.config import load_config


def test_load_config_defaults_when_no_options_file(monkeypatch, tmp_path: Path) -> None:
    # Point the loader at a non-existent options file and an isolated config dir.
    monkeypatch.setenv("MYLO_OPTIONS_FILE", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setenv("MYLO_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    cfg = load_config()

    assert cfg.llm_provider == "anthropic"
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.reconciliation_model == "claude-haiku-4-5-20251001"
    assert cfg.sync_frequency == "nightly"
    assert cfg.memory_token_limit == 8000
    assert cfg.proactive_notifications is True
    assert cfg.supervisor_token is None
    assert cfg.supervisor_token_present is False
    assert cfg.ha_config_dir == tmp_path
    assert cfg.mylo_data_dir == tmp_path / ".mylo"
    assert cfg.api_key == ""
    assert cfg.context_budget_factor == 0.6
    assert cfg.context_output_reserve_tokens == 8000
    assert cfg.working_set_max_entities == 40


def test_load_config_reads_options_file(monkeypatch, tmp_path: Path) -> None:
    opts = tmp_path / "options.json"
    opts.write_text(
        json.dumps(
            {
                "api_key": "sk-test",
                "llm_provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "sync_frequency": "weekly",
                "memory_token_limit": 12000,
                "proactive_notifications": False,
                "quiet_hours_start": "23:00",
            }
        )
    )
    monkeypatch.setenv("MYLO_OPTIONS_FILE", str(opts))
    monkeypatch.setenv("MYLO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "xyz")

    cfg = load_config()

    assert cfg.api_key == "sk-test"
    assert cfg.sync_frequency == "weekly"
    assert cfg.memory_token_limit == 12000
    assert cfg.proactive_notifications is False
    assert cfg.quiet_hours_start == "23:00"
    assert cfg.supervisor_token == "xyz"
    assert cfg.supervisor_token_present is True
