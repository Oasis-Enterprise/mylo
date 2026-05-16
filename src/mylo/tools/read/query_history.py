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

"""``query_history`` — retrieve entity state history over time.

Mylo can see current state but not trends. This tool queries HA's
history API to answer: "show me the kitchen temperature over the
last 48 hours", "when was the front door last unlocked?", "how
much energy did the dryer use this week?"

Uses HA's ``history/history_during_period`` websocket command which
returns a list of state changes per entity within a time window.
The raw data is summarized for the model — full state-change lists
would blow the token budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError
from mylo.logging_setup import get_logger
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)


class QueryHistoryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(
        description="The entity to query history for.",
    )
    hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="How many hours of history to retrieve (max 168 = 1 week).",
    )
    summary: bool = Field(
        default=True,
        description=(
            "If true, return a compact summary (first/last/min/max/count). "
            "If false, return the raw state change list (can be large)."
        ),
    )


async def handler(params: QueryHistoryParams, ctx: ToolContext) -> ToolResult:
    # Validate entity exists.
    if params.entity_id not in ctx.registries.entities:
        from mylo.tools.read.query_entities import _resolve_area_id  # noqa: F401

        return ToolResult.error(
            "entity_not_found",
            f"{params.entity_id} not in registry",
        )

    now = datetime.now(UTC)
    start = now - timedelta(hours=params.hours)

    try:
        result = await ctx.ws_client.send_command(
            "history/history_during_period",
            start_time=start.isoformat(),
            end_time=now.isoformat(),
            entity_ids=[params.entity_id],
            minimal_response=True,
            no_attributes=True,
            timeout=30.0,
        )
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    if not isinstance(result, dict):
        return ToolResult.error("unexpected_response", "history API returned unexpected format")

    states = result.get(params.entity_id, [])
    if not isinstance(states, list):
        states = []

    if not states:
        return ToolResult.ok(
            {
                "entity_id": params.entity_id,
                "hours": params.hours,
                "changes": 0,
                "message": f"No state changes recorded in the last {params.hours} hours.",
            }
        )

    if params.summary:
        return ToolResult.ok(_summarize(params.entity_id, states, params.hours))

    # Raw mode — cap at 100 entries to avoid token bloat.
    capped = states[:100]
    return ToolResult.ok(
        {
            "entity_id": params.entity_id,
            "hours": params.hours,
            "changes": len(states),
            "truncated": len(states) > 100,
            "states": [
                {
                    "state": s.get("s") or s.get("state", ""),
                    "last_changed": s.get("lc") or s.get("last_changed", ""),
                }
                for s in capped
            ],
        }
    )


def _summarize(entity_id: str, states: list[dict[str, Any]], hours: int) -> dict[str, Any]:
    """Build a compact summary of state changes for the model."""
    changes = len(states)

    # Extract state values.
    raw_values: list[str] = []
    for s in states:
        val = s.get("s") or s.get("state", "")
        if val and val not in ("unavailable", "unknown"):
            raw_values.append(str(val))

    first_state = raw_values[0] if raw_values else "—"
    last_state = raw_values[-1] if raw_values else "—"

    # Try to parse as numeric for min/max/avg.
    import contextlib

    numeric: list[float] = []
    for v in raw_values:
        with contextlib.suppress(TypeError, ValueError):
            numeric.append(float(v))

    summary: dict[str, Any] = {
        "entity_id": entity_id,
        "hours": hours,
        "changes": changes,
        "first_state": first_state,
        "last_state": last_state,
    }

    if numeric:
        summary["min"] = round(min(numeric), 2)
        summary["max"] = round(max(numeric), 2)
        summary["avg"] = round(sum(numeric) / len(numeric), 2)
        summary["readings"] = len(numeric)
    else:
        # For binary/discrete entities, show unique states and time in each.
        unique = sorted(set(raw_values))
        summary["unique_states"] = unique
        # Count time in each state (rough: based on number of entries).
        state_counts: dict[str, int] = {}
        for v in raw_values:
            state_counts[v] = state_counts.get(v, 0) + 1
        summary["state_distribution"] = state_counts

    # First and last timestamps.
    first_ts = states[0].get("lc") or states[0].get("last_changed", "")
    last_ts = states[-1].get("lc") or states[-1].get("last_changed", "")
    if first_ts:
        summary["first_change"] = first_ts
    if last_ts:
        summary["last_change"] = last_ts

    return summary


TOOL = ToolDefinition(
    name="query_history",
    description=(
        "Query an entity's state history over time. Returns state changes "
        "within a time window (default 24h, max 1 week). Use summary=true "
        "(default) for a compact overview (first/last/min/max/avg/count), "
        "or summary=false for raw state changes (capped at 100). Good for: "
        "'show me the temperature trend', 'when was the door last unlocked', "
        "'how often did the motion sensor trigger today'."
    ),
    params_model=QueryHistoryParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
