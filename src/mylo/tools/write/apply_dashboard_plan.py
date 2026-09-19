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

"""``apply_dashboard_plan`` — execute a plan the user approved.

The executor's tier-2 gate already requires the turn-wide ``approved``
flag. On top of that the plan id must be in ``approved_plan_ids`` —
the set the UI sends when the user clicks Apply on a plan card — so
the model cannot apply a different plan than the one shown.

All ops are applied in memory, a backup of the pre-apply config is
written, one ``lovelace/config/save`` is issued, and the result is
read back and verified against the plan.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.dashboard.backup import write_backup
from mylo.dashboard.io import (
    DashboardNotFound,
    DashboardUnavailable,
    fetch_dashboard_config,
    save_dashboard_config,
)
from mylo.dashboard.ops import OpError, OpReceipt, TargetMismatch, apply_op
from mylo.dashboard.verify import verify_plan
from mylo.ha.ws_client import CommandError
from mylo.logging_setup import get_logger
from mylo.tools import executor as tool_executor
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)


class ApplyDashboardPlanParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(
        min_length=1,
        description="plan_id from plan_dashboard, after the user clicked Apply on it.",
    )


async def handler(params: ApplyDashboardPlanParams, ctx: ToolContext) -> ToolResult:
    if params.plan_id not in ctx.approved_plan_ids:
        return ToolResult.error(
            "plan_not_approved",
            "this plan was not approved by the user — present it and wait for them to click Apply",
        )
    if ctx.plans is None:
        return ToolResult.error("plans_unavailable", "plan store not configured")
    plan = ctx.plans.get(params.plan_id)
    if plan is None or plan.conversation_id != ctx.conversation_id:
        return ToolResult.error(
            "plan_not_found",
            "plan expired or unknown — call plan_dashboard again and re-present it",
        )

    try:
        current = await fetch_dashboard_config(ctx.ws_client, plan.dashboard_id)
    except DashboardNotFound as exc:
        return ToolResult.error("dashboard_not_found", exc.message)
    except DashboardUnavailable as exc:
        return ToolResult.error("dashboard_unavailable", f"{exc.code}: {exc.message}")

    work: dict[str, Any] = current
    receipts: list[OpReceipt] = []
    try:
        for op, target in zip(plan.operations, plan.resolved, strict=True):
            work, receipt = apply_op(work, op, target)
            receipts.append(receipt)
    except TargetMismatch as exc:
        return ToolResult.error(
            "target_changed",
            exc.message,
            data={
                "op_index": exc.op_index,
                "expected": exc.expected.model_dump(),
                "actual": exc.actual.model_dump(),
                "hint": "the dashboard changed since the plan was made — query_dashboard and plan again",
            },
        )
    except OpError as exc:
        return ToolResult.error(exc.code, exc.message)

    backup_path: str | None = None
    try:
        backup_path = str(write_backup(ctx.config.mylo_data_dir, plan.dashboard_id, current))
    except OSError as exc:
        log.warning("dashboard.backup_failed", error=str(exc))

    try:
        await save_dashboard_config(ctx.ws_client, plan.dashboard_id, work)
    except CommandError as exc:
        return ToolResult.error(
            "ha_error", f"{exc.code}: {exc.message}", data={"backup": backup_path}
        )

    # The save changed what query_dashboard would report — drop any cached
    # reads so the model's next look at the dashboard is fresh.
    tool_executor.invalidate("query_dashboard")

    ctx.plans.remove(plan.plan_id)

    try:
        after = await fetch_dashboard_config(ctx.ws_client, plan.dashboard_id)
        verification = verify_plan(plan, receipts, work, after)
    except DashboardUnavailable as exc:
        verification = {
            "all_ok": False,
            "ops": [],
            "reason": f"could not read the dashboard back: {exc.code}",
        }

    log.info(
        "dashboard.plan_applied",
        plan_id=plan.plan_id,
        ops=len(receipts),
        all_ok=verification.get("all_ok"),
        backup=backup_path,
    )
    return ToolResult.ok(
        {
            "preview": False,
            "plan_id": plan.plan_id,
            "applied": len(receipts),
            "backup": backup_path,
            "verification": verification,
        }
    )


TOOL = ToolDefinition(
    name="apply_dashboard_plan",
    description=(
        "Apply a plan the user approved by clicking Apply on the plan card. "
        "Pass the plan_id from plan_dashboard. Writes a backup, saves once, "
        "reads the dashboard back, and reports per-operation verification. "
        "Refused unless the user's message carried approval for this plan_id."
    ),
    params_model=ApplyDashboardPlanParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
