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

"""Ollama API key handling (GitHub issue #14).

Plain Ollama ignores the key, but an authenticating gateway in front of
it needs the configured key sent as the bearer token.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from mylo.config import AppConfig, load_config
from mylo.llm.ollama_provider import OllamaProvider
from mylo.server.app import _create_provider


def test_configured_key_is_sent() -> None:
    p = OllamaProvider(base_url="http://gw:4000/v1", api_key="sk-gateway")
    assert p._client.api_key == "sk-gateway"


def test_blank_key_falls_back_to_placeholder() -> None:
    assert OllamaProvider(base_url="http://x/v1")._client.api_key == "ollama"
    assert OllamaProvider(base_url="http://x/v1", api_key="")._client.api_key == "ollama"


@pytest.fixture
def ollama_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AppConfig:
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"llm_provider": "ollama", "ollama_url": "http://gw:4000/v1"}))
    monkeypatch.setenv("MYLO_OPTIONS_FILE", str(opts))
    monkeypatch.setenv("MYLO_CONFIG_DIR", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OLLAMA_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(var, raising=False)
    return load_config()


def test_factory_passes_config_key(ollama_config: AppConfig) -> None:
    cfg = dataclasses.replace(ollama_config, api_key="sk-gateway")
    assert _create_provider(cfg)._client.api_key == "sk-gateway"


def test_factory_env_key_overrides_config(
    ollama_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-from-env")
    cfg = dataclasses.replace(ollama_config, api_key="sk-gateway")
    assert _create_provider(cfg)._client.api_key == "sk-from-env"


def test_factory_never_sends_anthropic_env_key(
    ollama_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    assert _create_provider(ollama_config)._client.api_key == "ollama"
