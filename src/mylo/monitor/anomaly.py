"""Z-score anomaly detection against baselines.

Runs during the hourly sweep. For each entity that has a stored
baseline (mean + stddev), fetch the current state value and compute
the z-score. If |z| > threshold, emit a finding for the notifier.

The default threshold is 2.5 sd — roughly a 1.2% chance of a false
positive per check on normally-distributed data. Sensors with high
natural variance (outdoor temperature, energy) will have wide stddev
from the 7-day baseline, so the threshold adapts organically.

Entities whose current state is non-numeric or ``unavailable`` are
silently skipped — the hourly availability sweep handles those.
"""

from __future__ import annotations

from typing import Any

from mylo.ha.states import get_all_states
from mylo.ha.ws_client import HaWsClient
from mylo.logging_setup import get_logger
from mylo.memory.schema import Baselines

log = get_logger(__name__)

Z_THRESHOLD = 2.5


async def check_anomalies(
    *,
    ws_client: HaWsClient,
    baselines: Baselines,
    threshold: float = Z_THRESHOLD,
) -> list[dict[str, Any]]:
    """Compare current sensor values against stored baselines.

    Returns a list of anomaly finding dicts suitable for the notifier.
    """
    if not baselines.entities:
        return []

    states = await get_all_states(ws_client)
    findings: list[dict[str, Any]] = []

    for baseline in baselines.entities:
        state_dict = states.get(baseline.entity)
        if state_dict is None:
            continue

        raw_value = state_dict.get("state")
        value = _to_float(raw_value)
        if value is None:
            continue

        if baseline.stddev <= 0:
            continue

        z = (value - baseline.avg) / baseline.stddev
        abs_z = abs(z)

        if abs_z < threshold:
            continue

        direction = "above" if z > 0 else "below"
        attrs = state_dict.get("attributes", {})
        unit = attrs.get("unit_of_measurement", "")
        friendly = attrs.get("friendly_name", baseline.entity)

        severity: str
        if abs_z >= 4.0:
            severity = "high"
        elif abs_z >= 3.0:
            severity = "normal"
        else:
            severity = "low"

        findings.append(
            {
                "id": baseline.entity.replace(".", "_"),
                "title": f"{friendly} is {abs_z:.1f}sd {direction} normal",
                "message": (
                    f"Current: {value}{unit} · "
                    f"Baseline: {baseline.avg:.1f}±{baseline.stddev:.1f}{unit} · "
                    f"Z-score: {z:+.2f}"
                ),
                "severity": severity,
                "entity_id": baseline.entity,
                "z_score": round(z, 2),
            }
        )

    if findings:
        log.info("anomaly.findings", count=len(findings))
    return findings


def _to_float(value: Any) -> float | None:
    """Try to parse a state value as float. Returns None on failure."""
    if value is None or value in ("unavailable", "unknown", ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
