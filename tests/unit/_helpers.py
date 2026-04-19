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

"""Shared test helpers. Kept underscore-prefixed so pytest doesn't collect
from here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mylo.config import AppConfig
from mylo.ha.registries import Registries
from mylo.safety.audit import AuditLogger
from mylo.safety.permissions import default_permissions
from mylo.tools.context import ToolContext


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        api_key="",
        llm_provider="anthropic",
        model="x",
        reconciliation_model="y",
        sync_frequency="nightly",
        memory_token_limit=8000,
        proactive_notifications=False,
        max_daily_notifications=3,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        session_budget_usd=0.50,
        monthly_budget_usd=15.00,
        supervisor_token=None,
        ha_config_dir=tmp_path,
        mylo_data_dir=tmp_path / ".mylo",
    )


def make_ctx(
    *,
    ws_client: Any,
    registries: Registries,
    tmp_path: Path,
    conversation_id: str = "test",
    user_approved: bool = False,
    dry_run: bool = False,
) -> ToolContext:
    config = make_config(tmp_path)
    return ToolContext(
        ws_client=ws_client,
        registries=registries,
        config=config,
        permissions=default_permissions(),
        audit=AuditLogger(config.mylo_data_dir),
        conversation_id=conversation_id,
        user_approved=user_approved,
        dry_run=dry_run,
    )
