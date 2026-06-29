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

"""Four-layer prompt assembler (spec §6.8).

Composes the full system prompt from:

* **Layer 1 — Identity**: the static ``system_prompt.txt`` (versioned).
* **Layer 2 — Home topology**: compressed YAML from live registries.
* **Layer 3 — Memory**: selectively-injected context file sections
  plus pending conflicts + scratchpad.
* **Layer 4 — Task references**: few-shot examples for the detected
  task type (automation / dashboard / troubleshoot / entity_management).

The assembler is intentionally stateless — it reads live registries,
memory, and the user turn each call. Caching lives in the caller
(the tool loop) via Anthropic's prompt-cache ephemeral marker on the
system block.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mylo.context.basic_prompt import LoadedPrompt, load_system_prompt
from mylo.context.budget import ContextBudget, Surface, TextSurface
from mylo.context.memory_injection import build_memory_section
from mylo.context.references import load_task_context
from mylo.context.selector import select_sections
from mylo.context.surfaces import TopologySurface
from mylo.context.task_detector import detect_task_type
from mylo.context.working_set import WorkingSetSurface
from mylo.ha.registries import Registries
from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile, Suggestion

log = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class AssembledPrompt:
    """Output of :func:`assemble_system_prompt`.

    ``system`` is the text the provider receives. ``prompt_version``
    stamps the conversation row for changelog correlation. ``task_type``
    and ``sections`` are surfaced for logging/debug — we want to see
    in traces which layer choices the assembler made on each turn.
    """

    system: str
    prompt_version: str
    task_type: str | None
    sections: frozenset[str]


def assemble_system_prompt(
    *,
    registries: Registries | None,
    memory: MemoryFile,
    conversation_text: str,
    mylo_data_dir: Path,
    timezone: str | None = None,
    base_prompt: LoadedPrompt | None = None,
    session_cost_usd: float = 0.0,
    session_budget_usd: float = 0.50,
    monthly_spent_usd: float = 0.0,
    monthly_budget_usd: float = 0.0,
    is_local_provider: bool = False,
    model: str = "claude-sonnet-4-6",
    budget_factor: float = 0.6,
    output_reserve: int = 8000,
    working_set_max_entities: int = 40,
) -> AssembledPrompt:
    """Build the full system prompt for one turn.

    ``conversation_text`` should be the latest user message (and
    optionally a short tail of prior turns). It drives both the
    memory selector and the task detector.

    ``registries`` may be None in degraded startup paths — the Layer 2
    topology block is skipped rather than crashing.

    ``base_prompt`` lets callers inject a pre-loaded prompt (tests do
    this); production paths let the default loader find the shipped
    ``system_prompt.txt``.
    """
    layer1 = base_prompt if base_prompt is not None else load_system_prompt()

    # Build priority-ordered surfaces, then let the budget render them so
    # the assembled prompt is provably bounded regardless of home size.
    # Highest priority first; conflicts are split out of the memory block
    # into their own high-priority surface so a safety-critical conflict is
    # never the thing budget pressure drops.
    surfaces: list[Surface] = [TextSurface("identity", layer1.text)]

    sections = select_sections(conversation_text, memory=memory)

    if "conflicts" in sections:
        conflicts_text = build_memory_section(
            memory, mylo_data_dir=mylo_data_dir, timezone=timezone, sections={"conflicts"}
        )
        if conflicts_text:
            surfaces.append(TextSurface("critical_memory", conflicts_text))

    # Layer 4 — Task references, only when we have a confident match.
    task_type = detect_task_type(conversation_text)
    if task_type is not None:
        references_text = load_task_context(task_type, mylo_data_dir=mylo_data_dir)
        if references_text:
            surfaces.append(
                TextSurface(
                    "task_refs",
                    f"REFERENCE EXAMPLES (task: {task_type}) — use these as "
                    f"few-shot patterns, not gospel:\n\n{references_text}",
                )
            )

    # Layer 2 — Home topology + relevance-ranked working set (elastic).
    if registries is not None and registries.entities:
        surfaces.append(TopologySurface(registries, memory=memory))
        surfaces.append(
            WorkingSetSurface(
                registries,
                conversation_text=conversation_text,
                monitored=set(memory.monitored_entities),
                states=None,
                max_entities=working_set_max_entities,
            )
        )

    # Layer 3 — remaining memory sections (conflicts rendered above).
    rest_sections = sections - {"conflicts"}
    memory_text = build_memory_section(
        memory, mylo_data_dir=mylo_data_dir, timezone=timezone, sections=rest_sections
    )
    if memory_text:
        surfaces.append(TextSurface("memory", memory_text))

    # Low-priority atomic blocks — first to drop under budget pressure.
    for name, text in _low_priority_blocks(
        memory,
        is_local_provider=is_local_provider,
        session_cost_usd=session_cost_usd,
        session_budget_usd=session_budget_usd,
        monthly_spent_usd=monthly_spent_usd,
        monthly_budget_usd=monthly_budget_usd,
    ):
        surfaces.append(TextSurface(name, text))

    budget = ContextBudget.for_model(model, factor=budget_factor, output_reserve=output_reserve)
    assembled = budget.render(surfaces)
    system = assembled.text

    log.info(
        "context.budget",
        prompt_version=layer1.version,
        task_type=task_type,
        total=budget.total_tokens,
        used=assembled.tokens_used,
        trimmed=assembled.trimmed,
        allocations=assembled.allocations,
    )

    return AssembledPrompt(
        system=system,
        prompt_version=layer1.version,
        task_type=task_type,
        sections=frozenset(sections),
    )


def _low_priority_blocks(
    memory: MemoryFile,
    *,
    is_local_provider: bool,
    session_cost_usd: float,
    session_budget_usd: float,
    monthly_spent_usd: float,
    monthly_budget_usd: float,
) -> list[tuple[str, str]]:
    """Build the low-priority prompt blocks as (name, text) pairs.

    These are nudges and notes that should be the first to drop when the
    context budget is tight, so they live below the load-bearing layers.
    """
    blocks: list[tuple[str, str]] = []

    hints = _cold_start_hints(memory)
    if hints:
        blocks.append(
            ("hints", "SETUP HINTS (mention naturally if relevant, don't force):\n" + hints)
        )

    pending = [pa for pa in memory.pending_actions if not pa.resolved]
    if pending:
        action_lines = [
            "PENDING OBSERVATIONS (mention these naturally at the start "
            "of the conversation — the user hasn't seen them yet. Offer "
            "to take action. After discussing, the user's response will "
            "be tracked as accepted/rejected):"
        ]
        for pa in pending:
            action_lines.append(f"- {pa.message}")
        blocks.append(("pending", "\n".join(action_lines)))

    proposals = _automation_proposals(memory)
    if proposals:
        blocks.append(("proposals", proposals))

    if not is_local_provider and session_budget_usd > 0 and session_cost_usd > 0:
        ratio = session_cost_usd / session_budget_usd
        if ratio >= 0.80:
            blocks.append(
                (
                    "cost_note",
                    f"COST NOTE: This session has used ${session_cost_usd:.2f} of "
                    f"the ${session_budget_usd:.2f} budget ({ratio:.0%}). "
                    "Mention this naturally if the user asks another complex "
                    "question. Prefer narrow queries and the topology summary "
                    "over broad entity scans to conserve tokens.",
                )
            )

    if not is_local_provider and monthly_budget_usd > 0 and monthly_spent_usd > 0:
        ratio = monthly_spent_usd / monthly_budget_usd
        if ratio >= 0.80:
            blocks.append(
                (
                    "monthly_cost_note",
                    f"MONTHLY COST NOTE: This month has used ${monthly_spent_usd:.2f} "
                    f"of the ${monthly_budget_usd:.2f} monthly budget ({ratio:.0%}). "
                    "Mention this naturally if the user asks another complex "
                    "question, and prefer cheaper, narrower queries.",
                )
            )

    return blocks


def _cold_start_hints(memory: MemoryFile) -> str:
    """Generate setup hints for sections the user hasn't configured yet."""
    hints: list[str] = []
    if not memory.monitored_entities:
        hints.append(
            "- No entities are being monitored yet. If the user asks about "
            "monitoring, energy, anomalies, or baselines, suggest setting up "
            "monitoring: use query_entities to find measurement sensors "
            "(state_class=measurement, energy, climate, battery), present "
            "candidates grouped by type, and use manage_monitored to add "
            "the ones they confirm."
        )
    if not memory.household.members:
        hints.append(
            "- No household members recorded. If the user mentions people "
            "by name or discusses presence/schedules, suggest recording "
            "them via memory_note so future conversations are personalized."
        )
    return "\n".join(hints)


def _automation_proposals(memory: MemoryFile) -> str:
    """Surface suggestions that are ready to become automations.

    When a proactive suggestion has been accepted 3+ times (the user
    keeps manually doing the same thing), inject a hint so the model
    offers to create an automation. The model uses modify_automation
    which it already knows how to do — this just tells it WHEN to
    offer.
    """
    ready = [s for s in memory.suggestions if not s.automated and s.times_accepted >= 3]
    if not ready:
        return ""

    lines = [
        "AUTOMATION PROPOSALS (the user has accepted these suggestions "
        "multiple times — proactively offer to create an automation so "
        "it happens automatically. If they agree, use modify_automation "
        "to build it, then tell the user it's done):"
    ]
    for s in ready:
        desc = _proposal_description(s)
        lines.append(f"- {desc}")

    return "\n".join(lines)


def _proposal_description(s: Suggestion) -> str:
    """Build a human-readable proposal from a suggestion."""
    if s.type in ("while_away", "on_while_away"):
        return (
            f"Turn off {s.entity_id} when everyone leaves home. "
            f"(You've accepted this {s.times_accepted} times.)"
        )
    if s.type == "duration_anomaly":
        return (
            f"Turn off {s.entity_id} when it's been on much longer than usual. "
            f"(You've accepted this {s.times_accepted} times.)"
        )
    return f"{s.description} (accepted {s.times_accepted} times)"
