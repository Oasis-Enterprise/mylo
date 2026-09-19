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

"""``plan_dashboard`` — validate a dashboard change and stage it for approval.

Nothing is written. The plan is stored server-side under a short id;
the UI renders it as a wireframe with Apply / Modify, and the Apply
click sends the id back as ``approved_plan_ids`` so
``apply_dashboard_plan`` can prove the user saw exactly this plan.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mylo.dashboard.card_schema import has_custom_card
from mylo.dashboard.io import (
    DashboardNotFound,
    DashboardUnavailable,
    fetch_dashboard_config,
)
from mylo.dashboard.plan import DashboardPlan, PlanDashboardParams
from mylo.dashboard.store import PlanStore
from mylo.dashboard.validate import new_cards_of, validate_plan
from mylo.ha.lovelace_meta import detect_custom_cards, get_resources, get_themes
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


async def handler(params: PlanDashboardParams, ctx: ToolContext) -> ToolResult:
    if ctx.plans is None:
        return ToolResult.error("plans_unavailable", "plan store not configured")

    try:
        current = await fetch_dashboard_config(ctx.ws_client, params.dashboard_id)
    except DashboardNotFound as exc:
        return ToolResult.error("dashboard_not_found", exc.message)
    except DashboardUnavailable as exc:
        return ToolResult.error("dashboard_unavailable", f"{exc.code}: {exc.message}")

    new_cards = [c for op in params.operations for c in new_cards_of(op)]
    installed_custom: set[str] | None = None
    if has_custom_card(new_cards):
        resources = await get_resources(ctx.ws_client)
        if resources is not None:
            installed_custom = detect_custom_cards(resources)

    theme_names: list[str] | None = None
    if any(getattr(op, "theme", None) for op in params.operations):
        themes = await get_themes(ctx.ws_client)
        theme_names = themes.names if themes is not None else None

    validation = validate_plan(
        params,
        current,
        registries=ctx.registries,
        installed_custom=installed_custom,
        theme_names=theme_names,
    )
    issues = [i.model_dump(exclude_none=True) for i in validation.issues]
    if not validation.ok:
        n_errors = sum(1 for i in validation.issues if i.severity == "error")
        return ToolResult.error(
            "plan_invalid",
            f"{n_errors} error(s) in the plan — fix every listed issue and call "
            "plan_dashboard again",
            data={"issues": issues},
        )

    plan = DashboardPlan(
        plan_id=PlanStore.new_id(),
        dashboard_id=params.dashboard_id,
        summary=params.summary,
        assumptions=params.assumptions,
        operations=params.operations,
        resolved=validation.resolved,
        issues=validation.issues,
        created_at=datetime.now(UTC),
        conversation_id=ctx.conversation_id,
    )
    ctx.plans.put(plan)
    return ToolResult.ok(
        {
            "preview": True,
            "plan_id": plan.plan_id,
            "plan": plan.model_dump(mode="json"),
            "entity_refs_validated": validation.entity_refs_checked,
            "issues": issues,
            "note": (
                "Plan staged. Tell the user it is ready to review, then END the turn. "
                "Call apply_dashboard_plan only after their next message approves it."
            ),
        }
    )


TOOL = ToolDefinition(
    name="plan_dashboard",
    description=(
        "Stage a dashboard change for the user's approval. Pass every operation "
        "for the change in one call: create_view (sections with headings), "
        "add_section, add_cards, replace_card, remove_card, move_card, "
        "update_view_meta, remove_section, delete_view. Sections are addressed "
        "by heading text or index; 'position' is 'start', 'end', or an index. "
        "Validates entity ids, card options, custom cards, and theme; returns "
        "plan_invalid with a fix list, or a plan_id the user must approve by "
        "clicking Apply. Nothing is written here. List every unasked choice in "
        "'assumptions'."
    ),
    params_model=PlanDashboardParams,
    tier=Tier.READ,
    handler=handler,
    cacheable=False,
)
register(TOOL)
