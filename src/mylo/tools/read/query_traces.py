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

"""``query_traces`` — inspect automation/script run traces.

Uses HA's ``trace/list`` and ``trace/get`` websocket commands to answer
"why did/didn't this automation or script run." Returns a compact summary of
recent runs and, for the latest (or a requested) run, shows what triggered it,
which steps executed, where it stopped, and any error.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class QueryTracesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_type: Literal["automation", "script"] = Field(
        default="automation",
        description="Whether to inspect an automation or a script.",
    )
    item_id: str = Field(
        description=(
            "The automation or script to inspect. Accepts the full entity_id "
            "(e.g. 'automation.morning_routine') or just the object_id "
            "('morning_routine')."
        ),
    )
    run_id: str | None = Field(
        default=None,
        description=(
            "A specific run_id to fetch. When omitted, returns the list of "
            "recent runs and a summary of the most recent one."
        ),
    )


def _normalize_item_id(item_type: str, item_id: str) -> str:
    """Strip the domain prefix if present, returning just the object_id.

    Accepts either 'automation.morning_routine' or 'morning_routine'.
    """
    prefix = f"{item_type}."
    if item_id.startswith(prefix):
        return item_id[len(prefix) :]
    return item_id


def _run_start(run: dict[str, Any]) -> str:
    """ISO-8601 start time of a run, '' if absent. Sorts correctly as a
    string (lexicographic == chronological for ISO-8601)."""
    ts = run.get("timestamp")
    if isinstance(ts, dict):
        return ts.get("start") or ""
    return ts or "" if isinstance(ts, str) else ""


def _summarize_run_list(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a compact list of runs (run_id, timestamps, state, error)."""
    out = []
    for run in runs:
        entry: dict[str, Any] = {
            "run_id": run.get("run_id"),
            "state": run.get("state"),
            "script_execution": run.get("script_execution"),
            "last_step": run.get("last_step"),
        }
        ts = run.get("timestamp") or {}
        if isinstance(ts, dict):
            entry["started"] = ts.get("start")
            entry["finished"] = ts.get("finish")
        else:
            entry["started"] = ts
        err = run.get("error")
        if err:
            entry["error"] = err
        out.append(entry)
    return out


def _summarize_trace(trace_data: dict[str, Any]) -> dict[str, Any]:
    """Produce a compact summary of a single full trace.

    Extracts: trigger, step list with outcomes, last_step, state, error.
    Avoids dumping the raw multi-KB trace dict.
    """
    summary: dict[str, Any] = {
        "run_id": trace_data.get("run_id"),
        "state": trace_data.get("state"),
        "script_execution": trace_data.get("script_execution"),
        "last_step": trace_data.get("last_step"),
    }

    ts = trace_data.get("timestamp") or {}
    if isinstance(ts, dict):
        summary["started"] = ts.get("start")
        summary["finished"] = ts.get("finish")

    # Trigger — compact: just the platform + key fields, not the full context.
    trigger = trace_data.get("trigger")
    if trigger and isinstance(trigger, dict):
        trigger_summary: dict[str, Any] = {}
        for key in ("platform", "entity_id", "at", "event_type", "zone"):
            if key in trigger:
                trigger_summary[key] = trigger[key]
        summary["trigger"] = trigger_summary
    elif trigger:
        summary["trigger"] = trigger

    # Steps — list the path keys and a one-line outcome for each.
    raw_trace = trace_data.get("trace") or {}
    steps: list[dict[str, Any]] = []
    for step_path, step_results in raw_trace.items():
        if not isinstance(step_results, list):
            continue
        for step in step_results:
            if not isinstance(step, dict):
                continue
            step_entry: dict[str, Any] = {"step": step_path}
            result = step.get("result")
            if isinstance(result, dict):
                # Pull out a few meaningful keys rather than the full result.
                for rk in ("result", "choice", "error", "exception"):
                    if rk in result:
                        step_entry[rk] = result[rk]
            step_error = step.get("error")
            if step_error:
                step_entry["error"] = step_error
            steps.append(step_entry)
    summary["steps_executed"] = steps

    error = trace_data.get("error")
    if error:
        summary["error"] = error

    return summary


async def handler(params: QueryTracesParams, ctx: ToolContext) -> ToolResult:
    domain = params.item_type
    item_id = _normalize_item_id(domain, params.item_id)

    # ── Path 1: explicit run_id — fetch that specific trace ──────────────────
    if params.run_id is not None:
        try:
            trace_data = await ctx.ws_client.send_command(
                "trace/get",
                domain=domain,
                item_id=item_id,
                run_id=params.run_id,
            )
        except CommandError as exc:
            return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

        if not isinstance(trace_data, dict):
            return ToolResult.error("unexpected_response", "trace/get returned non-dict response")

        return ToolResult.ok(
            {
                "domain": domain,
                "item_id": item_id,
                "run": _summarize_trace(trace_data),
            }
        )

    # ── Path 2: no run_id — list runs, summarize the most recent ─────────────
    try:
        runs_raw = await ctx.ws_client.send_command(
            "trace/list",
            domain=domain,
            item_id=item_id,
        )
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    runs = runs_raw if isinstance(runs_raw, list) else []

    if not runs:
        return ToolResult.ok(
            {
                "domain": domain,
                "item_id": item_id,
                "no_traces": True,
                "recent_runs": [],
                "message": (
                    f"No trace history found for {domain}.{item_id}. "
                    "The automation/script may never have run, or traces may have been cleared."
                ),
            }
        )

    # HA's trace/list returns runs OLDEST-first (insertion order in a
    # size-limited OrderedDict). Sort newest-first by start time so
    # "latest_run" and recent_runs are genuinely most-recent-first.
    runs = sorted(runs, key=_run_start, reverse=True)
    recent_runs = _summarize_run_list(runs)

    # Fetch full trace for the most recent run.
    most_recent_run_id = runs[0].get("run_id")
    latest_summary: dict[str, Any] | None = None
    if most_recent_run_id:
        try:
            trace_data = await ctx.ws_client.send_command(
                "trace/get",
                domain=domain,
                item_id=item_id,
                run_id=most_recent_run_id,
            )
            if isinstance(trace_data, dict):
                latest_summary = _summarize_trace(trace_data)
        except CommandError:
            # Non-fatal: we still have the list.
            pass

    result: dict[str, Any] = {
        "domain": domain,
        "item_id": item_id,
        "total_recent_runs": len(runs),
        "recent_runs": recent_runs,
    }
    if latest_summary is not None:
        result["latest_run"] = latest_summary

    return ToolResult.ok(result)


TOOL = ToolDefinition(
    name="query_traces",
    description=(
        "Inspect recent run traces for an automation or script to answer "
        "'why did/didn't this run.' Returns a list of recent runs "
        "(run_id, state, timestamps, error) and a compact summary of the "
        "latest run: what triggered it, which steps executed, where it "
        "stopped, and any error. Supply run_id to inspect a specific run."
    ),
    params_model=QueryTracesParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
