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
from mylo.context.memory_injection import build_memory_section
from mylo.context.references import load_task_context
from mylo.context.selector import select_sections
from mylo.context.task_detector import detect_task_type
from mylo.context.topology import build_topology, format_topology
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
    is_local_provider: bool = False,
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

    parts: list[str] = [layer1.text]

    # Layer 2 — Home topology.
    if registries is not None and registries.entities:
        topology = build_topology(registries, memory=memory)
        parts.append(format_topology(topology))

    # Layer 3 — Memory (selective).
    sections = select_sections(conversation_text, memory=memory)
    memory_text = build_memory_section(
        memory,
        mylo_data_dir=mylo_data_dir,
        timezone=timezone,
        sections=sections,
    )
    if memory_text:
        parts.append(memory_text)

    # Layer 4 — Task references, only when we have a confident match.
    task_type = detect_task_type(conversation_text)
    if task_type is not None:
        references_text = load_task_context(task_type, mylo_data_dir=mylo_data_dir)
        if references_text:
            parts.append(
                f"REFERENCE EXAMPLES (task: {task_type}) — use these as "
                f"few-shot patterns, not gospel:\n\n{references_text}"
            )

    # Cold-start hints — lightweight nudges that help the model guide
    # new users toward useful setup steps. Only surface when the
    # relevant section is genuinely empty.
    hints = _cold_start_hints(memory)
    if hints:
        parts.append("SETUP HINTS (mention naturally if relevant, don't force):\n" + hints)

    # Pending actions — proactive findings from the hourly sweep that
    # haven't been shown to the user yet. Mention them naturally at
    # the start of conversation ("I noticed while you were away...").
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
        parts.append("\n".join(action_lines))

    # Automation proposals — suggestions that have been accepted
    # enough times that we should offer to create an automation.
    proposals = _automation_proposals(memory)
    if proposals:
        parts.append(proposals)

    # Budget warning — when session cost approaches the configured cap,
    # tell the model so it can mention it naturally. Skipped for local
    # providers (Ollama) where cost is $0.
    if not is_local_provider and session_budget_usd > 0 and session_cost_usd > 0:
        ratio = session_cost_usd / session_budget_usd
        if ratio >= 0.80:
            parts.append(
                f"COST NOTE: This session has used ${session_cost_usd:.2f} of "
                f"the ${session_budget_usd:.2f} budget ({ratio:.0%}). "
                "Mention this naturally if the user asks another complex "
                "question. Prefer narrow queries and the topology summary "
                "over broad entity scans to conserve tokens."
            )

    system = "\n\n---\n\n".join(parts)

    log.debug(
        "context.assembled",
        prompt_version=layer1.version,
        task_type=task_type,
        sections=sorted(sections),
        system_chars=len(system),
    )

    return AssembledPrompt(
        system=system,
        prompt_version=layer1.version,
        task_type=task_type,
        sections=frozenset(sections),
    )


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
