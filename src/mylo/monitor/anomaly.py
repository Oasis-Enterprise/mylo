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

# Sensors with very low natural variance (batteries, stable temps)
# produce tiny stddevs where any small change looks like a spike.
# Clamp stddev to at least this fraction of the baseline mean so
# stable sensors don't fire on noise.
MIN_STDDEV_FRACTION = 0.02  # 2% of the mean

# Even if the z-score is high, ignore changes smaller than this
# absolute amount. A 0.6% battery drop isn't worth a notification.
MIN_ABSOLUTE_CHANGE = 5.0


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

        # Clamp stddev so stable sensors (batteries, slow-changing
        # temps) don't trigger on tiny fluctuations.
        effective_stddev = max(baseline.stddev, abs(baseline.avg) * MIN_STDDEV_FRACTION)

        z = (value - baseline.avg) / effective_stddev
        abs_z = abs(z)

        if abs_z < threshold:
            continue

        # Skip if the absolute change is too small to matter.
        if abs(value - baseline.avg) < MIN_ABSOLUTE_CHANGE:
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
                "title": f"{friendly}: unusually {'high' if z > 0 else 'low'}",
                "message": (
                    f"Current: {value}{unit} · "
                    f"Baseline: {baseline.avg:.1f} ± {baseline.stddev:.1f}{unit} · "
                    f"{abs_z:.1f} standard deviations {direction} normal"
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
