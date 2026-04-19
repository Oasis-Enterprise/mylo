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

"""``manage_monitored`` — add, remove, or list monitored entities.

Monitored entities feed two downstream systems:

* **Baselines** (nightly) — compute mean/stddev from HA's long-term
  statistics for each monitored sensor.
* **Anomaly detection** (hourly) — z-score current values against
  those baselines and notify on spikes.

The tool lets the model curate the list conversationally rather than
asking the user to hand-edit ``context.yaml``. Typical flow: the
model calls ``query_entities`` to discover measurement sensors, then
calls this tool with ``action="add"`` after the user confirms.

Tier-1 for ``list``; ``add``/``remove``/``replace`` require approval
(checked in-handler like ``manage_labels``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.logging_setup import get_logger
from mylo.memory.store import MemoryStore
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)

Action = Literal["add", "remove", "replace", "list"]


class ManageMonitoredParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(
        description=(
            "'list' shows current monitored entities. "
            "'add' appends entity_ids. 'remove' drops them. "
            "'replace' sets the list to exactly the given entity_ids."
        ),
    )
    entity_ids: list[str] = Field(
        default_factory=list,
        description="Entity IDs to add/remove/replace. Ignored for 'list'.",
    )


async def handler(params: ManageMonitoredParams, ctx: ToolContext) -> ToolResult:
    store = MemoryStore(mylo_data_dir=ctx.config.mylo_data_dir)
    await store.load()
    memory = store.current()

    if params.action == "list":
        return ToolResult.ok(
            {
                "monitored_entities": memory.monitored_entities,
                "count": len(memory.monitored_entities),
            }
        )

    if not ctx.user_approved:
        return ToolResult.error(
            "confirmation_required",
            f"'{params.action}' requires user approval — click Apply",
        )

    if not params.entity_ids and params.action != "replace":
        return ToolResult.error(
            "missing_param",
            f"'{params.action}' requires at least one entity_id",
        )

    # Validate that the entity_ids exist in the registry.
    invalid = [eid for eid in params.entity_ids if eid not in ctx.registries.entities]
    if invalid:
        return ToolResult.error(
            "entity_not_found",
            f"unknown entity_ids: {', '.join(invalid[:5])}",
            data={"invalid": invalid},
        )

    current = set(memory.monitored_entities)

    if params.action == "add":
        added = [eid for eid in params.entity_ids if eid not in current]
        current.update(params.entity_ids)
        change_note = f"add {len(added)} monitored entities"
    elif params.action == "remove":
        removed = [eid for eid in params.entity_ids if eid in current]
        current -= set(params.entity_ids)
        change_note = f"remove {len(removed)} monitored entities"
    elif params.action == "replace":
        current = set(params.entity_ids)
        change_note = f"replace monitored entities ({len(current)} total)"
    else:
        return ToolResult.error("invalid_action", f"unknown action {params.action!r}")

    memory.monitored_entities = sorted(current)
    await store.save(memory, note=change_note)

    return ToolResult.ok(
        {
            "action": params.action,
            "monitored_entities": memory.monitored_entities,
            "count": len(memory.monitored_entities),
        }
    )


TOOL = ToolDefinition(
    name="manage_monitored",
    description=(
        "Add, remove, or list entities in Mylo's monitoring list. "
        "Monitored entities get nightly baseline statistics and hourly "
        "anomaly detection. Use query_entities to discover good candidates "
        "(sensors with state_class=measurement, energy, climate, battery). "
        "'list' is free; add/remove/replace require approval."
    ),
    params_model=ManageMonitoredParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
