"""``query_dashboard`` — retrieve Lovelace dashboard configurations.

HA exposes two kinds of dashboards:

* **Storage mode** — edited in the HA UI, fetched via ``lovelace/config``
  with a ``url_path`` argument. The default dashboard uses ``url_path=null``.
* **YAML mode** — configured in ``configuration.yaml`` (or a package); fetched
  via file read. For M2 we only surface the storage-mode path; YAML-mode
  dashboards will flow through ``read_config_file`` once that tool lands.

The ``lovelace/dashboards/list`` command enumerates all non-default dashboards.
Combined with the default (``url_path=None``), that's the full list.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class QueryDashboardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dashboard_id: str | None = Field(
        default=None,
        description=(
            "Dashboard url_path (e.g. 'lovelace', 'overview'). Omit to list "
            "all dashboards without fetching their configs."
        ),
    )
    view_id: str | None = Field(
        default=None,
        description=(
            "If set along with ``dashboard_id``, return only the single view "
            "with this ``path`` from the dashboard config."
        ),
    )


async def _list_dashboards(ctx: ToolContext) -> list[dict[str, Any]]:
    try:
        raw = await ctx.ws_client.send_command("lovelace/dashboards/list")
    except CommandError:
        raw = []
    items: list[dict[str, Any]] = [
        {
            "dashboard_id": d.get("url_path"),
            "title": d.get("title"),
            "mode": d.get("mode"),
            "icon": d.get("icon"),
            "require_admin": d.get("require_admin", False),
            "show_in_sidebar": d.get("show_in_sidebar", True),
        }
        for d in (raw or [])
        if isinstance(d, dict)
    ]
    # Always include the default dashboard.
    items.insert(
        0,
        {
            "dashboard_id": None,
            "title": "Overview",
            "mode": "storage",
            "icon": "mdi:view-dashboard",
            "default": True,
        },
    )
    return items


async def _fetch_config(ctx: ToolContext, dashboard_id: str | None) -> Any:
    # ``url_path`` accepts null for the default dashboard.
    return await ctx.ws_client.send_command("lovelace/config", url_path=dashboard_id)


def _view_summary(view: dict[str, Any]) -> dict[str, Any]:
    cards = view.get("cards") or []
    return {
        "path": view.get("path"),
        "title": view.get("title"),
        "icon": view.get("icon"),
        "card_count": len(cards) if isinstance(cards, list) else 0,
    }


async def handler(params: QueryDashboardParams, ctx: ToolContext) -> ToolResult:
    if params.dashboard_id is None and params.view_id is None:
        dashboards = await _list_dashboards(ctx)
        return ToolResult.ok({"dashboards": dashboards, "count": len(dashboards)})

    try:
        config = await _fetch_config(ctx, params.dashboard_id)
    except CommandError as exc:
        if exc.code in ("config_not_found", "not_found"):
            return ToolResult.error(
                "dashboard_not_found",
                f"no dashboard with url_path {params.dashboard_id!r}",
            )
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    if not isinstance(config, dict):
        return ToolResult.error(
            "unexpected_response",
            f"lovelace/config returned {type(config).__name__}",
        )

    views = config.get("views") or []
    if params.view_id:
        for v in views:
            if isinstance(v, dict) and v.get("path") == params.view_id:
                return ToolResult.ok(
                    {
                        "dashboard_id": params.dashboard_id,
                        "view": v,
                    }
                )
        return ToolResult.error(
            "view_not_found",
            f"view {params.view_id!r} not in dashboard {params.dashboard_id!r}",
            data={"available": [v.get("path") for v in views if isinstance(v, dict)]},
        )

    return ToolResult.ok(
        {
            "dashboard_id": params.dashboard_id,
            "title": config.get("title"),
            "view_count": len(views),
            "views": [_view_summary(v) for v in views if isinstance(v, dict)],
        }
    )


TOOL = ToolDefinition(
    name="query_dashboard",
    description=(
        "Retrieve Lovelace dashboard configurations. With no parameters, "
        "lists all dashboards. With ``dashboard_id``, returns view summaries "
        "for that dashboard. With ``view_id`` too, returns the full single "
        "view. Storage-mode dashboards only in this version; YAML-mode "
        "dashboards are read via ``read_config_file``."
    ),
    params_model=QueryDashboardParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
