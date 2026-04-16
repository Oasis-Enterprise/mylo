"""Layer 2 — compressed home-topology summary (spec §6.4).

The topology is a YAML-style summary of the home: per-area domain
counts, integration breakdowns, device counts. It's ~200-400 tokens
on a 2000-entity home — small enough to always include, specific
enough that the model stops asking "what's in the kitchen?" every turn.

Generated fresh from the in-memory :class:`Registries` on each call.
That's O(entities + devices) and registries stay warm, so per-turn
regeneration is cheaper than any cache-invalidation scheme we'd have
to write. Memory notes tagged with an ``area`` get surfaced inline —
"workshop conversion in progress" under `garage:` — so the model
factors them in without separate lookup.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from mylo.ha.registries import Registries
from mylo.memory.schema import MemoryFile


def build_topology(registries: Registries, memory: MemoryFile | None = None) -> dict[str, Any]:
    """Return a dict summarizing the home. Render via :func:`format_topology`."""
    area_names = {aid: area.name for aid, area in registries.areas.items()}

    # Domain + integration counts per area.
    per_area_domains: dict[str | None, Counter[str]] = defaultdict(Counter)
    per_area_integrations: dict[str | None, Counter[str]] = defaultdict(Counter)
    total_by_domain: Counter[str] = Counter()
    integration_totals: Counter[str] = Counter()

    # Device area lookup falls back to the entity's device's area when
    # the entity itself is unassigned — matches HA's own UI semantics.
    for entity in registries.entities.values():
        if entity.disabled_by or entity.hidden_by:
            continue
        area_id = entity.area_id
        if not area_id and entity.device_id:
            device = registries.devices.get(entity.device_id)
            if device:
                area_id = device.area_id
        per_area_domains[area_id][entity.domain] += 1
        total_by_domain[entity.domain] += 1
        if entity.platform:
            per_area_integrations[area_id][entity.platform] += 1
            integration_totals[entity.platform] += 1

    # Memory notes keyed by area so the topology carries them inline.
    area_notes: dict[str, list[str]] = defaultdict(list)
    if memory is not None:
        for note in memory.notes:
            if note.area:
                area_notes[note.area].append(note.content)

    # Device counts per area.
    devices_per_area: Counter[str | None] = Counter()
    for device in registries.devices.values():
        if device.disabled_by:
            continue
        devices_per_area[device.area_id] += 1

    areas_block: dict[str, Any] = {}
    # Sort areas by active entity count (desc) so the most populated
    # show up first — when the model gets truncated, we want signal up top.
    sorted_area_ids = sorted(
        [aid for aid in area_names],
        key=lambda aid: -sum(per_area_domains.get(aid, Counter()).values()),
    )
    for aid in sorted_area_ids:
        name = area_names[aid]
        domains = per_area_domains.get(aid, Counter())
        integrations = per_area_integrations.get(aid, Counter())
        if not domains and not devices_per_area.get(aid):
            # Skip empty areas — they're noise in the prompt.
            continue
        entry: dict[str, Any] = {}
        if domains:
            entry["domains"] = _format_counter(domains)
        if devices_per_area.get(aid):
            entry["devices"] = devices_per_area[aid]
        if integrations:
            entry["integrations"] = sorted(integrations)
        notes = area_notes.get(name) or area_notes.get(aid)
        if notes:
            entry["notes"] = notes
        areas_block[name] = entry

    # Unassigned bucket (entities that are neither area-tagged nor
    # tied to an area-tagged device). Knowing this number helps Mylo
    # suggest area-assignment work later.
    unassigned_entities = sum(per_area_domains.get(None, Counter()).values())
    if unassigned_entities:
        areas_block["unassigned"] = {
            "entities": unassigned_entities,
            "note": f"{unassigned_entities} entities not assigned to any area",
        }

    return {
        "total_entities": sum(
            1 for e in registries.entities.values() if not e.disabled_by and not e.hidden_by
        ),
        "total_devices": sum(1 for d in registries.devices.values() if not d.disabled_by),
        "total_areas": len(registries.areas),
        "total_automations": total_by_domain.get("automation", 0),
        "total_scripts": total_by_domain.get("script", 0),
        "areas": areas_block,
        "integrations": _format_counter(integration_totals),
    }


def format_topology(topology: dict[str, Any]) -> str:
    """Render a topology dict as a compact human-readable block.

    YAML-ish but hand-rolled so we control exact layout and don't
    emit ``!python/object`` tags from counters.
    """
    lines: list[str] = ["HOME TOPOLOGY:"]
    lines.append(f"- total_entities: {topology['total_entities']}")
    lines.append(f"- total_devices: {topology['total_devices']}")
    lines.append(f"- total_areas: {topology['total_areas']}")
    if topology.get("total_automations"):
        lines.append(f"- total_automations: {topology['total_automations']}")
    if topology.get("total_scripts"):
        lines.append(f"- total_scripts: {topology['total_scripts']}")

    areas = topology.get("areas") or {}
    if areas:
        lines.append("areas:")
        for name, entry in areas.items():
            lines.append(f"  {name}:")
            if "domains" in entry:
                lines.append(f"    {entry['domains']}")
            if "devices" in entry:
                lines.append(f"    devices: {entry['devices']}")
            if "integrations" in entry:
                lines.append(f"    integrations: [{', '.join(entry['integrations'])}]")
            if "notes" in entry:
                for note in entry["notes"]:
                    lines.append(f"    note: {note}")
            if "entities" in entry:  # unassigned bucket
                lines.append(f"    entities: {entry['entities']}")
            if "note" in entry:
                lines.append(f"    note: {entry['note']}")

    integrations = topology.get("integrations")
    if integrations:
        lines.append(f"integrations: {integrations}")

    return "\n".join(lines)


def _format_counter(counter: Counter[str]) -> str:
    """Render ``{'light': 4, 'sensor': 6}`` → ``lights=4, sensors=6``."""
    if not counter:
        return ""
    # Pluralize common domain names for readability. Not exhaustive —
    # exotic domains just get the bare domain label.
    parts = [f"{_pluralize(domain)}={count}" for domain, count in counter.most_common()]
    return ", ".join(parts)


_PLURALS = {
    "light": "lights",
    "switch": "switches",
    "sensor": "sensors",
    "binary_sensor": "binary_sensors",
    "cover": "covers",
    "lock": "locks",
    "fan": "fans",
    "climate": "climate",
    "media_player": "media_players",
    "camera": "cameras",
    "vacuum": "vacuums",
    "script": "scripts",
    "automation": "automations",
    "scene": "scenes",
    "device_tracker": "device_trackers",
    "person": "persons",
    "input_boolean": "input_booleans",
    "input_number": "input_numbers",
    "input_select": "input_selects",
    "input_text": "input_texts",
    "input_datetime": "input_datetimes",
    "number": "numbers",
    "select": "selects",
    "button": "buttons",
    "update": "updates",
    "weather": "weather",
    "sun": "sun",
}


def _pluralize(domain: str) -> str:
    return _PLURALS.get(domain, domain)
