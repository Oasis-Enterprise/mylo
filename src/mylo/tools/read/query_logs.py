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

"""``query_logs`` — retrieve state history, logbook entries, or system logs.

Three distinct data sources unified behind one tool:

* ``state_history`` — ``history/history_during_period`` command. Raw state
  transitions per entity.
* ``logbook`` — ``logbook/get_events``. Human-readable events filtered by
  entity or free-form.
* ``system_log`` — ``system_log/list``. Python log entries from HA core.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

LogType = Literal["state_history", "logbook", "system_log"]


class QueryLogsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: LogType = Field(
        description="Which log source to query.",
    )
    entity_id: str | None = Field(
        default=None,
        description=(
            "Filter to a single entity. Required for state_history; optional "
            "for logbook; ignored for system_log."
        ),
    )
    hours: int = Field(
        default=6,
        ge=1,
        le=24 * 30,
        description=(
            "How far back to look, in hours. Default 6 — raise deliberately "
            "for historical queries, since larger windows can be slow on busy "
            "homes."
        ),
    )
    severity: str | None = Field(
        default=None,
        description=(
            "For system_log only: 'ERROR', 'WARNING', 'INFO', 'DEBUG' (case-insensitive)."
        ),
    )
    limit: int = Field(default=200, ge=1, le=2000)


def _since_iso(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


async def _state_history(ctx: ToolContext, params: QueryLogsParams) -> ToolResult:
    if not params.entity_id:
        return ToolResult.error("missing_param", "entity_id is required for state_history")
    start = _since_iso(params.hours)
    try:
        raw = await ctx.ws_client.send_command(
            "history/history_during_period",
            start_time=start,
            end_time=datetime.now(UTC).isoformat(),
            entity_ids=[params.entity_id],
            minimal_response=True,
            no_attributes=True,
        )
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    # HA returns either a list-of-lists (legacy) or dict keyed by entity_id.
    rows: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        series = raw.get(params.entity_id, [])
    elif isinstance(raw, list):
        series = raw[0] if raw else []
    else:
        series = []
    if isinstance(series, list):
        for point in series[: params.limit]:
            if isinstance(point, dict):
                rows.append(
                    {
                        "state": point.get("state") or point.get("s"),
                        "last_changed": point.get("last_changed") or point.get("lc"),
                    }
                )

    return ToolResult.ok(
        {
            "entity_id": params.entity_id,
            "hours": params.hours,
            "points": len(rows),
            "history": rows,
        }
    )


async def _logbook(ctx: ToolContext, params: QueryLogsParams) -> ToolResult:
    cmd: dict[str, Any] = {
        "start_time": _since_iso(params.hours),
        "end_time": datetime.now(UTC).isoformat(),
    }
    if params.entity_id:
        cmd["entity_ids"] = [params.entity_id]
    try:
        raw = await ctx.ws_client.send_command("logbook/get_events", **cmd)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    items = raw if isinstance(raw, list) else []
    trimmed = items[: params.limit]
    return ToolResult.ok(
        {
            "hours": params.hours,
            "entity_id": params.entity_id,
            "count": len(items),
            "events": trimmed,
        }
    )


async def _system_log(ctx: ToolContext, params: QueryLogsParams) -> ToolResult:
    try:
        raw = await ctx.ws_client.send_command("system_log/list")
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    items = raw if isinstance(raw, list) else []
    if params.severity:
        want = params.severity.upper()
        items = [i for i in items if isinstance(i, dict) and (i.get("level", "")).upper() == want]
    trimmed = items[: params.limit]

    # Trim large fields — system logs include full tracebacks and we don't
    # want to flood the LLM context on every call.
    shaped = []
    for entry in trimmed:
        if not isinstance(entry, dict):
            continue
        shaped.append(
            {
                "level": entry.get("level"),
                "source": entry.get("source"),
                "message": entry.get("message"),
                "timestamp": entry.get("timestamp"),
                "count": entry.get("count", 1),
                "exception": (entry.get("exception") or "")[:400] or None,
            }
        )
    return ToolResult.ok(
        {
            "severity": params.severity,
            "count": len(items),
            "entries": shaped,
        }
    )


_DISPATCH = {
    "state_history": _state_history,
    "logbook": _logbook,
    "system_log": _system_log,
}


async def handler(params: QueryLogsParams, ctx: ToolContext) -> ToolResult:
    fn = _DISPATCH[params.type]
    return await fn(ctx, params)


TOOL = ToolDefinition(
    name="query_logs",
    description=(
        "Retrieve logs for troubleshooting. 'state_history' returns entity "
        "state transitions (requires entity_id). 'logbook' returns "
        "human-readable events, optionally filtered by entity. 'system_log' "
        "returns HA core Python log entries, optionally filtered by severity."
    ),
    params_model=QueryLogsParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
