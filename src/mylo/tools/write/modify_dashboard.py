"""``modify_dashboard`` — create, update, delete dashboard views and cards.

Storage-mode dashboards are edited via HA's ``lovelace/config/save``
websocket command. The full dashboard config is read, modified in
memory, then written back. For M7b this is the only path; YAML-mode
dashboards flow through ``write_config_file`` / ``patch_config_file``.

Dry-run returns a structural diff (via :mod:`mylo.files.diff`); apply
writes the updated config. No file ops or reload needed — HA's
frontend picks up Lovelace changes immediately on save.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.diff import diff_structs
from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

Action = Literal["create", "update", "delete"]


class ModifyDashboardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(
        description="'create' a new view, 'update' an existing view/card, 'delete' a view."
    )
    dashboard_id: str | None = Field(
        default=None,
        description=(
            "Dashboard url_path. null = default Overview dashboard. Required for all operations."
        ),
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "For 'create': a full view config {title, path, icon?, cards}. "
            "For 'update': the complete replacement dashboard config "
            "(all views). Use query_dashboard to read current, modify, pass "
            "back. For 'delete': not needed (use view_path instead)."
        ),
    )
    view_path: str | None = Field(
        default=None,
        description="For 'delete': the view path to remove from the dashboard.",
    )
    dry_run: bool = Field(default=True)


async def _fetch_dashboard(ctx: ToolContext, dashboard_id: str | None) -> dict[str, Any] | None:
    """Read the current dashboard config from HA."""
    try:
        result = await ctx.ws_client.send_command("lovelace/config", url_path=dashboard_id)
        return result if isinstance(result, dict) else None
    except CommandError:
        return None


async def _save_dashboard(
    ctx: ToolContext, dashboard_id: str | None, config: dict[str, Any]
) -> None:
    """Write the full dashboard config back to HA."""
    await ctx.ws_client.send_command("lovelace/config/save", url_path=dashboard_id, config=config)


async def handler(params: ModifyDashboardParams, ctx: ToolContext) -> ToolResult:
    if params.action == "create":
        return await _create_view(ctx, params)
    if params.action == "update":
        return await _update_dashboard(ctx, params)
    if params.action == "delete":
        return await _delete_view(ctx, params)
    return ToolResult.error("invalid_action", f"unknown action {params.action!r}")


async def _create_view(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    if not params.config:
        return ToolResult.error("missing_param", "create requires 'config' (a view definition)")

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        current = {"views": []}

    new_view = params.config
    views = list(current.get("views") or [])

    # Check for duplicate path.
    new_path = new_view.get("path")
    if new_path and any(v.get("path") == new_path for v in views if isinstance(v, dict)):
        return ToolResult.error(
            "already_exists",
            f"view with path {new_path!r} already exists in this dashboard",
        )

    views.append(new_view)
    new_config = {**current, "views": views}

    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "create",
        "view_path": new_path,
        "view_title": new_view.get("title"),
        "card_count": len(new_view.get("cards") or []),
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


async def _update_dashboard(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    if not params.config:
        return ToolResult.error(
            "missing_param",
            "update requires 'config' (the full replacement dashboard config)",
        )

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    new_config = params.config

    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "update",
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


async def _delete_view(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    if not params.view_path:
        return ToolResult.error("missing_param", "delete requires 'view_path'")

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        return ToolResult.error("dashboard_not_found", "dashboard not found")

    views = list(current.get("views") or [])
    new_views = [
        v for v in views if not (isinstance(v, dict) and v.get("path") == params.view_path)
    ]

    if len(new_views) == len(views):
        return ToolResult.error(
            "view_not_found",
            f"no view with path {params.view_path!r}",
            data={"available": [v.get("path") for v in views if isinstance(v, dict)]},
        )

    new_config = {**current, "views": new_views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "delete",
        "view_path": params.view_path,
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


TOOL = ToolDefinition(
    name="modify_dashboard",
    description=(
        "Create, update, or delete Lovelace dashboard views and cards. "
        "Storage-mode dashboards only (edited via HA's websocket API). "
        "For YAML dashboards, use write_config_file / patch_config_file. "
        "ALWAYS call with dry_run=true first, then apply after user "
        "approval. Changes are immediate — no reload needed."
    ),
    params_model=ModifyDashboardParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
