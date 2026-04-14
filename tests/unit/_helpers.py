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
