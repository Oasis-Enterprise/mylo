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

"""Tests for M4c: four-layer context assembler.

Covers topology rendering, keyword-based memory selection, task
detection, reference loading, and end-to-end assembler composition.
"""

from __future__ import annotations

from pathlib import Path

from mylo.context.assembler import assemble_system_prompt
from mylo.context.basic_prompt import LoadedPrompt
from mylo.context.memory_injection import build_memory_section
from mylo.context.references import available_references, load_task_context
from mylo.context.selector import ALWAYS_ON, select_sections
from mylo.context.task_detector import detect_task_type
from mylo.context.tokens import estimate_tokens
from mylo.context.topology import build_topology, format_topology
from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries
from mylo.memory.schema import HouseholdMember, Note, empty_memory


def _fake_registries() -> Registries:
    """Build a synthetic :class:`Registries` without touching HA."""
    r = Registries()
    r.areas = {
        "kitchen": AreaEntry(area_id="kitchen", name="Kitchen", floor_id=None, labels=()),
        "garage": AreaEntry(area_id="garage", name="Garage", floor_id=None, labels=()),
    }
    r.devices = {
        "d_kitchen": DeviceEntry(
            id="d_kitchen",
            name="Kitchen Ecobee",
            name_by_user=None,
            manufacturer="ecobee",
            model="3",
            area_id="kitchen",
            labels=(),
            disabled_by=None,
        ),
    }
    r.entities = {
        "light.kitchen_overhead": EntityEntry(
            entity_id="light.kitchen_overhead",
            name="Overhead",
            original_name="Overhead",
            platform="hue",
            device_id=None,
            area_id="kitchen",
            labels=(),
            disabled_by=None,
            hidden_by=None,
        ),
        "sensor.kitchen_temperature": EntityEntry(
            entity_id="sensor.kitchen_temperature",
            name=None,
            original_name="Kitchen temperature",
            platform="ecobee",
            device_id="d_kitchen",
            area_id=None,  # via device
            labels=(),
            disabled_by=None,
            hidden_by=None,
        ),
        "light.garage_workshop": EntityEntry(
            entity_id="light.garage_workshop",
            name="Workshop",
            original_name="Workshop",
            platform="zwave_js",
            device_id=None,
            area_id="garage",
            labels=(),
            disabled_by=None,
            hidden_by=None,
        ),
        "automation.morning": EntityEntry(
            entity_id="automation.morning",
            name=None,
            original_name="Morning routine",
            platform="automation",
            device_id=None,
            area_id=None,
            labels=(),
            disabled_by=None,
            hidden_by=None,
        ),
        "sensor.disabled": EntityEntry(
            entity_id="sensor.disabled",
            name=None,
            original_name="disabled",
            platform="hue",
            device_id=None,
            area_id="kitchen",
            labels=(),
            disabled_by="user",
            hidden_by=None,
        ),
    }
    return r


# ─── Topology ───────────────────────────────────────────────────────────────


def test_topology_counts_entities_by_area_and_domain() -> None:
    r = _fake_registries()
    topology = build_topology(r)

    # disabled entity excluded from totals
    assert topology["total_entities"] == 4
    assert topology["total_automations"] == 1

    kitchen = topology["areas"]["Kitchen"]
    assert "lights=1" in kitchen["domains"]
    assert "sensors=1" in kitchen["domains"]
    assert "ecobee" in kitchen["integrations"] or "hue" in kitchen["integrations"]


def test_topology_uses_device_area_when_entity_has_none() -> None:
    r = _fake_registries()
    topology = build_topology(r)
    kitchen = topology["areas"]["Kitchen"]
    # sensor.kitchen_temperature has no area_id but its device is in kitchen.
    assert "sensors=1" in kitchen["domains"]


def test_topology_pulls_area_notes_from_memory() -> None:
    r = _fake_registries()
    mem = empty_memory()
    mem.notes.append(Note(id="n1", content="workshop conversion in progress", area="Garage"))
    topology = build_topology(r, memory=mem)
    garage = topology["areas"]["Garage"]
    assert garage["notes"] == ["workshop conversion in progress"]


def test_format_topology_renders_readable_text() -> None:
    r = _fake_registries()
    topology = build_topology(r)
    rendered = format_topology(topology)
    assert "HOME TOPOLOGY:" in rendered
    assert "total_entities: 4" in rendered
    assert "Kitchen:" in rendered


# ─── Selector ───────────────────────────────────────────────────────────────


def test_selector_always_includes_core_sections() -> None:
    sections = select_sections("what time is it", empty_memory())
    for core in ALWAYS_ON:
        assert core in sections


def test_selector_triggers_known_issues_on_problem_keyword() -> None:
    sections = select_sections("my basement motion sensor is not working", empty_memory())
    assert "known_issues" in sections


def test_selector_triggers_patterns_on_usually_keyword() -> None:
    sections = select_sections("we usually turn off lights at 11pm", empty_memory())
    assert "patterns" in sections


def test_selector_triggers_baselines_on_energy_keyword() -> None:
    sections = select_sections("is my energy usage unusual today?", empty_memory())
    assert "baselines" in sections


def test_selector_always_includes_conflicts_when_pending() -> None:
    from mylo.memory.schema import Claim, Conflict

    mem = empty_memory()
    mem.conflicts.append(
        Conflict(
            id="c1",
            type="contradiction",
            claim_a=Claim(content="x", source="memory"),
            claim_b=Claim(content="y", source="scratchpad"),
            status="pending_review",
        )
    )
    sections = select_sections("turn on the kitchen lights", mem)
    assert "conflicts" in sections


def test_selector_word_boundary_prevents_false_positives() -> None:
    # "fixture" should not match "fix".
    sections = select_sections("replace the kitchen fixture", empty_memory())
    assert "known_issues" not in sections


# ─── Task detector ──────────────────────────────────────────────────────────


def test_task_detector_picks_automation() -> None:
    assert detect_task_type("build an automation that triggers when I arrive") == "automation"


def test_task_detector_picks_dashboard() -> None:
    assert detect_task_type("add a dashboard card for the living room lights") == "dashboard"


def test_task_detector_picks_troubleshoot() -> None:
    assert detect_task_type("why isn't my motion sensor working — it says unavailable") == (
        "troubleshoot"
    )


def test_task_detector_picks_entity_management() -> None:
    assert detect_task_type("help me rename and organize the kitchen entities") == (
        "entity_management"
    )


def test_task_detector_returns_none_for_casual_state_queries() -> None:
    assert detect_task_type("what lights are on in the kitchen") is None


# ─── References ────────────────────────────────────────────────────────────


def test_load_task_context_returns_package_references(tmp_path: Path) -> None:
    text = load_task_context("automation", mylo_data_dir=tmp_path)
    assert "automation_examples.yaml" in text
    assert "trigger:" in text


def test_user_references_override_package(tmp_path: Path) -> None:
    refs = tmp_path / "references"
    refs.mkdir(parents=True)
    (refs / "automation_examples.yaml").write_text("# user-custom starter\n- id: my_override\n")
    text = load_task_context("automation", mylo_data_dir=tmp_path)
    assert "user-custom starter" in text
    # The original package content must NOT be in the result when user overrides.
    assert "lights_off_at_bedtime" not in text


def test_available_references_introspection(tmp_path: Path) -> None:
    out = available_references(mylo_data_dir=tmp_path)
    assert "automation" in out
    assert len(out["automation"]) >= 1


# ─── memory_injection section filter ───────────────────────────────────────


def test_memory_injection_respects_sections_filter(tmp_path: Path) -> None:
    mem = empty_memory()
    mem.household.members.append(HouseholdMember(name="Alice", role="primary_user"))
    mem.notes.append(Note(id="n1", content="a note"))

    # Only render household — notes section should NOT appear.
    text = build_memory_section(
        mem,
        mylo_data_dir=tmp_path,
        sections={"household"},
    )
    assert "Alice" in text
    assert "a note" not in text


# ─── Assembler end-to-end ──────────────────────────────────────────────────


def test_assembler_includes_all_four_layers(tmp_path: Path) -> None:
    r = _fake_registries()
    mem = empty_memory()
    mem.household.members.append(HouseholdMember(name="Alice", role="primary_user"))

    # Use a fake base prompt so the test doesn't depend on the shipped file.
    base = LoadedPrompt(text="# Layer 1 identity block.", version="test-1.0")

    result = assemble_system_prompt(
        registries=r,
        memory=mem,
        conversation_text="build an automation that turns lights off when I'm away",
        mylo_data_dir=tmp_path,
        base_prompt=base,
    )

    assert "# Layer 1 identity block." in result.system
    assert "HOME TOPOLOGY:" in result.system
    assert "Alice" in result.system  # memory layer (household)
    assert "REFERENCE EXAMPLES" in result.system  # task layer
    assert result.task_type == "automation"
    assert result.prompt_version == "test-1.0"
    assert "household" in result.sections


def test_assembler_skips_topology_without_registries(tmp_path: Path) -> None:
    mem = empty_memory()
    base = LoadedPrompt(text="identity", version="v")
    result = assemble_system_prompt(
        registries=None,
        memory=mem,
        conversation_text="hello",
        mylo_data_dir=tmp_path,
        base_prompt=base,
    )
    assert "HOME TOPOLOGY:" not in result.system


def test_assembled_prompt_respects_budget(tmp_path: Path) -> None:
    reg = _fake_registries()
    result = assemble_system_prompt(
        registries=reg,
        memory=empty_memory(),
        conversation_text="lights",
        mylo_data_dir=tmp_path,
        base_prompt=LoadedPrompt(text="identity", version="v"),
        model="claude-haiku-4-5",
        budget_factor=0.0009,  # 200_000 * 0.0009 = 180 tokens
        output_reserve=0,
    )
    assert estimate_tokens(result.system) <= 180
    # Identity is highest priority — it survives even a tiny budget.
    assert "identity" in result.system


def test_assembler_skips_references_when_no_task(tmp_path: Path) -> None:
    mem = empty_memory()
    base = LoadedPrompt(text="identity", version="v")
    result = assemble_system_prompt(
        registries=None,
        memory=mem,
        conversation_text="what time is it",
        mylo_data_dir=tmp_path,
        base_prompt=base,
    )
    assert "REFERENCE EXAMPLES" not in result.system
    assert result.task_type is None


def _assemble_with_monthly(tmp_path: Path, *, spent: float, budget: float, local: bool = False):
    return assemble_system_prompt(
        registries=None,
        memory=empty_memory(),
        conversation_text="hello",
        mylo_data_dir=tmp_path,
        base_prompt=LoadedPrompt(text="identity", version="v"),
        monthly_spent_usd=spent,
        monthly_budget_usd=budget,
        is_local_provider=local,
    )


def test_monthly_warning_fires_at_80_percent(tmp_path: Path) -> None:
    result = _assemble_with_monthly(tmp_path, spent=12.0, budget=15.0)
    assert "MONTHLY COST NOTE" in result.system
    assert "$12.00" in result.system
    assert "80%" in result.system


def test_monthly_warning_silent_below_threshold(tmp_path: Path) -> None:
    result = _assemble_with_monthly(tmp_path, spent=5.0, budget=15.0)
    assert "MONTHLY COST NOTE" not in result.system


def test_monthly_warning_skipped_for_local_provider(tmp_path: Path) -> None:
    # Local models are free; a monthly $ warning is meaningless.
    result = _assemble_with_monthly(tmp_path, spent=99.0, budget=15.0, local=True)
    assert "MONTHLY COST NOTE" not in result.system


def test_monthly_warning_off_when_budget_zero(tmp_path: Path) -> None:
    result = _assemble_with_monthly(tmp_path, spent=99.0, budget=0.0)
    assert "MONTHLY COST NOTE" not in result.system
