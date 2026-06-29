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

"""Add-on configuration loader.

HA Supervisor writes add-on options to ``/data/options.json`` inside the
container and exposes the Supervisor token via the ``SUPERVISOR_TOKEN``
environment variable. In local dev we fall back to ``MYLO_OPTIONS_FILE`` or
a sensible set of defaults so ``python -m mylo`` runs anywhere.

Option schema is duplicated in ``addon/config.yaml`` — keep them in sync.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SyncFrequency = Literal["nightly", "weekly", "manual"]
LLMProvider = Literal["anthropic", "openai", "gemini", "ollama"]
NotificationMethod = Literal["auto", "mobile", "persistent", "off"]

DEFAULT_OPTIONS_PATH = Path("/data/options.json")

# Paths inside the HA config share. The new 'homeassistant_config' map
# mounts at /homeassistant; the legacy 'config' map used /config. We
# check both and prefer whichever exists, overridable via MYLO_CONFIG_DIR.
DEFAULT_HA_CONFIG_DIR = Path("/homeassistant")
LEGACY_HA_CONFIG_DIR = Path("/config")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Resolved, validated runtime configuration."""

    api_key: str
    llm_provider: LLMProvider
    model: str
    reconciliation_model: str
    ollama_url: str
    sync_frequency: SyncFrequency
    memory_token_limit: int
    proactive_notifications: bool
    max_daily_notifications: int
    quiet_hours_start: str
    quiet_hours_end: str
    session_budget_usd: float
    monthly_budget_usd: float
    context_budget_factor: float
    context_output_reserve_tokens: int
    working_set_max_entities: int
    notification_method: NotificationMethod

    # Runtime-only (not in user options)
    supervisor_token: str | None
    ha_config_dir: Path
    mylo_data_dir: Path

    @property
    def supervisor_token_present(self) -> bool:
        return bool(self.supervisor_token)


def _read_options() -> dict[str, Any]:
    path_str = os.environ.get("MYLO_OPTIONS_FILE")
    path = Path(path_str) if path_str else DEFAULT_OPTIONS_PATH
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"options file at {path} must contain a JSON object")
    return data


def _get(options: dict[str, Any], key: str, default: Any) -> Any:
    value = options.get(key, default)
    # HA Supervisor sometimes passes through empty strings; treat as unset.
    if isinstance(value, str) and value == "":
        return default
    return value


def load_config() -> AppConfig:
    options = _read_options()

    ha_config_dir_env = os.environ.get("MYLO_CONFIG_DIR")
    if ha_config_dir_env:
        ha_config_dir = Path(ha_config_dir_env)
    elif DEFAULT_HA_CONFIG_DIR.exists():
        ha_config_dir = DEFAULT_HA_CONFIG_DIR
    elif LEGACY_HA_CONFIG_DIR.exists():
        ha_config_dir = LEGACY_HA_CONFIG_DIR
    else:
        ha_config_dir = DEFAULT_HA_CONFIG_DIR
    mylo_data_dir = ha_config_dir / ".mylo"

    llm_provider: LLMProvider = _get(options, "llm_provider", "anthropic")
    sync_frequency: SyncFrequency = _get(options, "sync_frequency", "nightly")

    if llm_provider not in ("anthropic", "openai", "gemini", "ollama"):
        raise ValueError(f"invalid llm_provider: {llm_provider}")
    if sync_frequency not in ("nightly", "weekly", "manual"):
        raise ValueError(f"invalid sync_frequency: {sync_frequency}")

    return AppConfig(
        api_key=_get(options, "api_key", ""),
        llm_provider=llm_provider,
        model=_get(options, "model", "claude-sonnet-4-6"),
        reconciliation_model=_get(options, "reconciliation_model", "claude-haiku-4-5-20251001"),
        ollama_url=str(_get(options, "ollama_url", "")),
        sync_frequency=sync_frequency,
        memory_token_limit=int(_get(options, "memory_token_limit", 8000)),
        proactive_notifications=bool(_get(options, "proactive_notifications", True)),
        max_daily_notifications=int(_get(options, "max_daily_notifications", 3)),
        quiet_hours_start=str(_get(options, "quiet_hours_start", "22:00")),
        quiet_hours_end=str(_get(options, "quiet_hours_end", "07:00")),
        session_budget_usd=float(_get(options, "session_budget_usd", 0.50)),
        monthly_budget_usd=float(_get(options, "monthly_budget_usd", 15.00)),
        context_budget_factor=float(_get(options, "context_budget_factor", 0.6)),
        context_output_reserve_tokens=int(_get(options, "context_output_reserve_tokens", 8000)),
        working_set_max_entities=int(_get(options, "working_set_max_entities", 40)),
        notification_method=_get(options, "notification_method", "auto"),
        supervisor_token=os.environ.get("SUPERVISOR_TOKEN"),
        ha_config_dir=ha_config_dir,
        mylo_data_dir=mylo_data_dir,
    )
