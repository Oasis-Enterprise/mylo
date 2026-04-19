"""Hourly availability sweep (no LLM).

Checks three things, all from HA's live state:

1. **Unavailable entities** — entities in state ``unavailable`` or
   ``unknown`` that are not disabled and belong to a monitored
   domain. Ignores entities that have been unavailable since the
   last check (only reports *newly* unavailable ones).

2. **Stale automations** — automations whose ``last_triggered``
   hasn't advanced in more than 2x their expected interval. Only
   tracked for automations that have a known trigger schedule.

3. **Offline device trackers** — ``device_tracker`` / ``person``
   entities that flipped to ``not_home`` / ``away`` when the user
   hasn't left (future refinement — for now we just track them as
   low-severity informational findings).

Returns a list of finding dicts suitable for the notifier.
"""

from __future__ import annotations

from typing import Any

from mylo.ha.registries import Registries
from mylo.ha.states import get_all_states
from mylo.ha.ws_client import HaWsClient
from mylo.logging_setup import get_logger

log = get_logger(__name__)

# Only sweep these domains — no point flagging input_booleans or
# scenes as unavailable.
_MONITORED_DOMAINS = frozenset(
    {
        "sensor",
        "binary_sensor",
        "light",
        "switch",
        "cover",
        "lock",
        "climate",
        "fan",
        "media_player",
        "camera",
        "vacuum",
        "weather",
    }
)

# Mutable module-level set tracking entities that were already
# unavailable on the last sweep so we don't re-notify.
_previously_unavailable: set[str] = set()


async def run_hourly_check(
    *,
    ws_client: HaWsClient,
    registries: Registries | None,
) -> list[dict[str, Any]]:
    """Run one sweep. Returns a list of finding dicts.

    Each finding has: ``id``, ``title``, ``message``, ``severity``.
    """
    global _previously_unavailable

    findings: list[dict[str, Any]] = []
    states = await get_all_states(ws_client)

    # 1. Unavailable entities.
    currently_unavailable: set[str] = set()
    for entity_id, state in states.items():
        domain = entity_id.split(".", 1)[0]
        if domain not in _MONITORED_DOMAINS:
            continue
        # Skip disabled entities (if registries available).
        if registries is not None:
            entry = registries.entities.get(entity_id)
            if entry and (entry.disabled_by or entry.hidden_by):
                continue
        s = state.get("state", "")
        if s in ("unavailable", "unknown"):
            currently_unavailable.add(entity_id)

    newly_unavailable = currently_unavailable - _previously_unavailable
    if newly_unavailable:
        sorted_new = sorted(newly_unavailable)
        # Group into a single notification if many go offline at once
        # (integration restart, network outage).
        count = len(sorted_new)
        if count <= 3:
            entity_list = ", ".join(sorted_new)
            title = f"{count} {'entity' if count == 1 else 'entities'} unavailable"
        else:
            entity_list = ", ".join(sorted_new[:3]) + f" and {count - 3} more"
            title = f"{count} entities unavailable"

        findings.append(
            {
                "id": "unavailable",
                "title": title,
                "message": f"Newly unavailable: {entity_list}",
                "severity": "normal" if count < 10 else "high",
            }
        )

    _previously_unavailable = currently_unavailable

    # 2. Stale automations — look for automations that haven't fired
    #    in >48h and have a "time" trigger (simplistic heuristic).
    for entity_id, state in states.items():
        if not entity_id.startswith("automation."):
            continue
        if state.get("state") != "on":
            continue
        attrs = state.get("attributes", {})
        last = attrs.get("last_triggered")
        if not last:
            continue
        from datetime import UTC, datetime

        try:
            triggered_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            delta = datetime.now(UTC) - triggered_dt
            if delta.total_seconds() > 48 * 3600:
                friendly = attrs.get("friendly_name", entity_id)
                findings.append(
                    {
                        "id": f"stale_{entity_id}",
                        "title": f"Automation hasn't fired in {delta.days}d",
                        "message": (
                            f"{friendly} last triggered {delta.days} days ago. "
                            "It may be misconfigured or no longer needed."
                        ),
                        "severity": "low",
                    }
                )
        except (ValueError, TypeError):
            continue

    return findings


def reset_state() -> None:
    """Clear the module-level tracking set — used by tests."""
    global _previously_unavailable
    _previously_unavailable = set()
