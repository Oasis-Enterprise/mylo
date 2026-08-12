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
the z-score. A finding is only emitted when |z| exceeds 3.5 sd *and*
the anomaly persists for at least 2 consecutive hourly checks — this
kills one-hour blips that were generating noise at the old 2.5 sd
threshold. Streaks reset to zero as soon as the value returns within
threshold or falls below MIN_ABSOLUTE_CHANGE.

Persistence streaks are strictly consecutive: checks where the entity
is unreadable (missing from states, non-numeric value, or zero stddev)
RESET the streak. This keeps "2 consecutive anomalous checks" meaningful
rather than counting across gaps.

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

Z_THRESHOLD = 3.5

# An anomaly must persist for this many consecutive hourly checks
# before a finding is emitted — kills one-hour blips.
PERSISTENCE_CHECKS = 2

# Per-entity count of consecutive anomalous checks.
_streaks: dict[str, int] = {}

# Sensors with very low natural variance (batteries, stable temps)
# produce tiny stddevs where any small change looks like a spike.
# Clamp stddev to at least this fraction of the baseline mean so
# stable sensors don't fire on noise.
MIN_STDDEV_FRACTION = 0.02  # 2% of the mean

# Even if the z-score is high, ignore changes smaller than this
# absolute amount. A 0.6% battery drop isn't worth a finding.
MIN_ABSOLUTE_CHANGE = 5.0


async def check_anomalies(
    *,
    ws_client: HaWsClient,
    baselines: Baselines,
    threshold: float = Z_THRESHOLD,
) -> list[dict[str, Any]]:
    """Compare current sensor values against stored baselines.

    Returns a list of anomaly finding dicts for the findings store.

    Maintains per-entity persistence streaks across calls (see module
    docstring). Calling with a non-default ``threshold`` perturbs that
    shared state — reserve custom thresholds for tests.
    """
    if not baselines.entities:
        # No baselines means every entity is unreadable this check —
        # stale streaks must not survive to fire on a single later hit.
        _streaks.clear()
        return []

    states = await get_all_states(ws_client)
    findings: list[dict[str, Any]] = []

    for baseline in baselines.entities:
        state_dict = states.get(baseline.entity)
        if state_dict is None:
            # an unreadable hour breaks the 'consecutive' chain by design —
            # better to under-alert than fire on stale streaks.
            _streaks.pop(baseline.entity, None)
            continue

        raw_value = state_dict.get("state")
        value = _to_float(raw_value)
        if value is None:
            _streaks.pop(baseline.entity, None)
            continue

        if baseline.stddev <= 0:
            _streaks.pop(baseline.entity, None)
            continue

        # Clamp stddev so stable sensors (batteries, slow-changing
        # temps) don't trigger on tiny fluctuations.
        effective_stddev = max(baseline.stddev, abs(baseline.avg) * MIN_STDDEV_FRACTION)

        z = (value - baseline.avg) / effective_stddev
        abs_z = abs(z)

        if abs_z < threshold or abs(value - baseline.avg) < MIN_ABSOLUTE_CHANGE:
            _streaks.pop(baseline.entity, None)
            continue

        streak = _streaks.get(baseline.entity, 0) + 1
        _streaks[baseline.entity] = streak
        if streak < PERSISTENCE_CHECKS:
            continue

        direction = "above" if z > 0 else "below"
        attrs = state_dict.get("attributes", {})
        unit = attrs.get("unit_of_measurement", "")
        friendly = attrs.get("friendly_name", baseline.entity)

        severity: str
        if abs_z >= 4.5:
            severity = "high"
        elif abs_z >= 4.0:
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

    # baselines are rebuilt nightly, so entities come and go — stale streaks
    # must not survive their entity's absence.
    current = {b.entity for b in baselines.entities}
    for key in list(_streaks.keys()):
        if key not in current:
            _streaks.pop(key)

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


def reset_state() -> None:
    """Clear persistence streaks — used by tests."""
    _streaks.clear()
