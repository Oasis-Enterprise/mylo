# Learned-Norms Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Mylo's noisy fixed-threshold monitor alerts with per-entity learned norms — entities stay silent until Mylo has enough history to know what's normal for them, and findings live in a bounded, self-cleaning store.

**Architecture:** A new `EntityProfile` (compact aggregates: duration histogram, exact max, away-behavior samples) is folded nightly from the existing 14-day `transitions.jsonl`. All hourly detectors gate on profile confidence. Findings replace the append-forever `pending_actions` semantics: keyed by `(type, entity_id)`, capped at 5, auto-resolving, 48h TTL, dismiss = 7-day cooldown.

**Tech Stack:** Python 3.12 (aiohttp, pydantic v2, APScheduler), pytest (asyncio auto mode), React/TypeScript UI. Spec: `docs/superpowers/specs/2026-06-11-learned-norms-engine-design.md`.

**Conventions for this repo:**
- Every new Python file starts with the Apache-2.0 header (copy the 13-line comment block from `src/mylo/monitor/anomaly.py:1-13`) and uses `from __future__ import annotations`.
- Logging: `from mylo.logging_setup import get_logger` / `log = get_logger(__name__)`, structured kwargs (`log.info("profiles.saved", count=3)`).
- Tests live in `tests/unit/`, async test functions need no decorator (asyncio auto mode). Run with `python -m pytest`.
- Lint/format: `ruff check src tests --fix && ruff format src tests` before each commit.
- Do NOT run `npm run build` — the user verifies UI changes via HA rebuild.

---

### Task 1: EntityProfile model + ProfileStore

**Files:**
- Create: `src/mylo/monitor/profiles.py`
- Test: `tests/unit/test_profiles.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_profiles.py` (with the Apache header comment block):

```python
"""Tests for learned entity profiles: store roundtrip, folding,
gates, and thresholds."""

from __future__ import annotations

from pathlib import Path

from mylo.monitor.profiles import (
    EntityProfile,
    ProfileSet,
    ProfileStore,
)


def test_profile_store_roundtrip(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    profile_set = ProfileSet(
        last_folded="2026-06-01T00:00:00+00:00",
        entities={
            "light.kitchen": EntityProfile(
                entity_id="light.kitchen",
                days_observed=15,
                cycle_count=20,
                max_duration_s=21600.0,
            )
        },
    )
    store.save(profile_set)

    loaded = store.load()
    assert loaded.last_folded == "2026-06-01T00:00:00+00:00"
    assert loaded.entities["light.kitchen"].cycle_count == 20
    assert loaded.entities["light.kitchen"].max_duration_s == 21600.0


def test_profile_store_missing_file_returns_empty(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    loaded = store.load()
    assert loaded.entities == {}
    assert loaded.last_folded is None


def test_profile_store_corrupt_file_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "profiles.json").write_text("{not valid json", encoding="utf-8")
    store = ProfileStore(tmp_path)
    loaded = store.load()
    assert loaded.entities == {}


def test_profile_defaults() -> None:
    p = EntityProfile(entity_id="switch.fan")
    assert p.days_observed == 0
    assert p.cycle_count == 0
    assert len(p.duration_histogram) == 10
    assert len(p.active_hours) == 24
    assert p.away_samples == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_profiles.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mylo.monitor.profiles'`

- [ ] **Step 3: Implement the model and store**

Create `src/mylo/monitor/profiles.py` (Apache header first):

```python
"""Per-entity learned behavior profiles.

The transition log (``transitions.jsonl``) holds 14 days of raw
state changes. Profiles are the compact, long-lived aggregates
folded out of that stream: how long an entity typically stays on,
the longest it has EVER stayed on, and how it behaves when nobody
is home. Detectors gate on these profiles — an entity earns the
right to alert by accumulating observations ("quiet until
confident").

Stored in ``{mylo_data_dir}/profiles.json`` — deliberately NOT in
``context.yaml``: this is machine state, and corruption is never
fatal (entities just re-enter the quiet period).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mylo.logging_setup import get_logger

log = get_logger(__name__)

# Duration histogram bucket upper bounds, in seconds:
# 5m, 15m, 30m, 1h, 2h, 4h, 8h, 16h, 24h — plus one open-ended >24h
# bucket. Coarse on purpose: enough to derive a p95 without ever
# storing raw durations.
BUCKET_EDGES_S: tuple[int, ...] = (300, 900, 1800, 3600, 7200, 14400, 28800, 57600, 86400)
NUM_BUCKETS = len(BUCKET_EDGES_S) + 1

# The state that starts a duration cycle, per domain. Domains not
# listed here (person, climate) get no duration tracking.
ACTIVE_STATES: dict[str, str] = {
    "light": "on",
    "switch": "on",
    "fan": "on",
    "media_player": "on",
    "lock": "unlocked",
    "cover": "open",
    "binary_sensor": "on",
}

# Eligibility gates — quiet until confident.
MIN_DAYS_FOR_DURATION = 14
MIN_CYCLES_FOR_DURATION = 8
MIN_AWAY_SAMPLES = 8
AWAY_RARE_FRACTION = 0.20
CONFIDENCE_FULL_DAYS = 21

# Duration must exceed BOTH the all-time max (with margin) and a
# multiple of the typical (p95) duration. Locks and doors get
# tighter margins — higher stakes.
DEFAULT_MARGINS = (1.25, 2.0)  # (max multiplier, p95 multiplier)
TIGHT_MARGINS = (1.1, 1.5)
TIGHT_DEVICE_CLASSES = frozenset({"door", "garage_door"})


class EntityProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    entity_id: str
    first_seen: str | None = None
    last_event_date: str | None = None  # YYYY-MM-DD of last counted event
    days_observed: int = 0
    total_events: int = 0
    cycle_count: int = 0
    max_duration_s: float = 0.0
    duration_histogram: list[int] = Field(default_factory=lambda: [0] * NUM_BUCKETS)
    active_hours: list[int] = Field(default_factory=lambda: [0] * 24)
    # Entered the active state but the cycle hasn't closed yet —
    # carries across fold runs so boundary-spanning cycles count.
    active_since: str | None = None
    away_samples: int = 0
    on_while_away_samples: int = 0


class ProfileSet(BaseModel):
    """Root of profiles.json: all profiles + the fold watermark."""

    model_config = ConfigDict(extra="allow")

    last_folded: str | None = None
    entities: dict[str, EntityProfile] = Field(default_factory=dict)


class ProfileStore:
    """Load/save profiles.json. Corruption is never fatal."""

    def __init__(self, mylo_data_dir: Path) -> None:
        self._path = mylo_data_dir / "profiles.json"

    def load(self) -> ProfileSet:
        if not self._path.exists():
            return ProfileSet()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return ProfileSet.model_validate(raw)
        except (OSError, ValueError) as exc:
            log.warning("profiles.load_failed", error=str(exc))
            return ProfileSet()

    def save(self, profile_set: ProfileSet) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: a torn write would silently discard weeks of
            # accumulated learning that the 14-day raw log can't rebuild.
            atomic_write(self._path, json.dumps(profile_set.model_dump()))
        except OSError as exc:
            log.warning("profiles.save_failed", error=str(exc))
```

(`atomic_write` comes from `mylo.files.manager` — the repo's standard tempfile+fsync+replace helper. Add the import at the top.)

Also add a length-normalizing validator to `EntityProfile` so a hand-edited or schema-drifted profiles.json can never IndexError the fold loop:

```python
    @field_validator("duration_histogram", "active_hours", mode="after")
    @classmethod
    def _normalize_length(cls, v: list[int], info: ValidationInfo) -> list[int]:
        expected = NUM_BUCKETS if info.field_name == "duration_histogram" else 24
        if len(v) < expected:
            return v + [0] * (expected - len(v))
        return v[:expected]
```

(imports: `from pydantic import ... field_validator, ValidationInfo`)
```

(`math` is imported now because Task 2 adds `p95_duration_s`; if ruff flags it as unused at this step, add it in Task 2 instead.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_profiles.py -v`
Expected: 4 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests --fix && ruff format src tests
git add src/mylo/monitor/profiles.py tests/unit/test_profiles.py
git commit -m "feat(monitor): EntityProfile model + ProfileStore for learned norms"
```

---

### Task 2: Fold transitions into profiles

**Files:**
- Modify: `src/mylo/monitor/profiles.py` (append functions)
- Test: `tests/unit/test_profiles.py` (append tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_profiles.py`:

```python
from mylo.monitor.profiles import (  # noqa: E402  (merge into the import at top)
    fold_transitions,
    p95_duration_s,
)
from mylo.monitor.transitions import Transition  # noqa: E402


def _t(entity_id: str, from_state: str, to_state: str, ts: str) -> Transition:
    return Transition(entity_id=entity_id, from_state=from_state, to_state=to_state, timestamp=ts)


def test_fold_records_completed_cycle() -> None:
    ps = ProfileSet()
    cycles = fold_transitions(
        ps,
        [
            _t("light.kitchen", "off", "on", "2026-06-01T06:30:00+00:00"),
            _t("light.kitchen", "on", "off", "2026-06-01T07:00:00+00:00"),
        ],
    )
    assert cycles == 1
    p = ps.entities["light.kitchen"]
    assert p.cycle_count == 1
    assert p.max_duration_s == 1800.0
    assert p.duration_histogram[2] == 1  # 30min lands in the ≤30m bucket
    assert p.days_observed == 1
    assert p.total_events == 2
    assert ps.last_folded == "2026-06-01T07:00:00+00:00"


def test_fold_watermark_prevents_double_counting() -> None:
    ps = ProfileSet()
    transitions = [
        _t("light.kitchen", "off", "on", "2026-06-01T06:30:00+00:00"),
        _t("light.kitchen", "on", "off", "2026-06-01T07:00:00+00:00"),
    ]
    fold_transitions(ps, transitions)
    cycles = fold_transitions(ps, transitions)  # same data again
    assert cycles == 0
    assert ps.entities["light.kitchen"].cycle_count == 1


def test_fold_cycle_spans_fold_boundary() -> None:
    ps = ProfileSet()
    fold_transitions(ps, [_t("light.kitchen", "off", "on", "2026-06-01T22:00:00+00:00")])
    assert ps.entities["light.kitchen"].active_since == "2026-06-01T22:00:00+00:00"
    cycles = fold_transitions(
        ps, [_t("light.kitchen", "on", "off", "2026-06-02T01:00:00+00:00")]
    )
    assert cycles == 1
    assert ps.entities["light.kitchen"].max_duration_s == 3 * 3600.0


def test_fold_ignores_unwatched_domains_and_counts_days() -> None:
    ps = ProfileSet()
    fold_transitions(
        ps,
        [
            _t("person.max", "home", "not_home", "2026-06-01T08:00:00+00:00"),
            _t("light.kitchen", "off", "on", "2026-06-01T08:30:00+00:00"),
            _t("light.kitchen", "on", "off", "2026-06-01T09:00:00+00:00"),
            _t("light.kitchen", "off", "on", "2026-06-02T08:30:00+00:00"),
            _t("light.kitchen", "on", "off", "2026-06-02T09:00:00+00:00"),
        ],
    )
    assert "person.max" not in ps.entities
    assert ps.entities["light.kitchen"].days_observed == 2
    assert ps.entities["light.kitchen"].cycle_count == 2


def test_p95_from_histogram() -> None:
    p = EntityProfile(entity_id="light.kitchen")
    # 20 cycles of ~30 min (bucket index 2: ≤1800s), one 6h outlier.
    p.duration_histogram[2] = 20
    p.duration_histogram[6] = 1  # ≤8h bucket
    p.cycle_count = 21
    p.max_duration_s = 21600.0
    # 95th percentile of 21 cycles = the 20th — still in the 30m bucket.
    assert p95_duration_s(p) == 1800.0


def test_p95_empty_histogram_is_zero() -> None:
    assert p95_duration_s(EntityProfile(entity_id="x")) == 0.0


def test_p95_last_bucket_uses_max_duration() -> None:
    p = EntityProfile(entity_id="x")
    p.duration_histogram[9] = 10  # everything >24h
    p.max_duration_s = 100000.0
    assert p95_duration_s(p) == 100000.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_profiles.py -v`
Expected: FAIL — `ImportError: cannot import name 'fold_transitions'`

- [ ] **Step 3: Implement folding and p95**

Append to `src/mylo/monitor/profiles.py` (add `from datetime import datetime` and `from mylo.monitor.transitions import Transition` to the imports):

```python
def fold_transitions(profile_set: ProfileSet, transitions: list[Transition]) -> int:
    """Fold new transitions into the profiles, in place.

    Only transitions newer than the watermark (``last_folded``) are
    processed, so re-reading the same 14-day window every night
    never double-counts. Returns the number of cycles recorded.
    """
    watermark = profile_set.last_folded or ""
    fresh = sorted(
        (t for t in transitions if t.timestamp > watermark),
        key=lambda t: t.timestamp,
    )
    cycles = 0

    for t in fresh:
        domain = t.entity_id.split(".", 1)[0]
        active = ACTIVE_STATES.get(domain)
        if active is None:
            continue

        profile = profile_set.entities.get(t.entity_id)
        if profile is None:
            profile = EntityProfile(entity_id=t.entity_id, first_seen=t.timestamp)
            profile_set.entities[t.entity_id] = profile

        profile.total_events += 1
        event_date = t.timestamp[:10]
        if event_date != profile.last_event_date:
            profile.days_observed += 1
            profile.last_event_date = event_date
        try:
            profile.active_hours[datetime.fromisoformat(t.timestamp).hour] += 1
        except ValueError:
            pass

        if t.to_state == active:
            profile.active_since = t.timestamp
        elif t.from_state == active and profile.active_since is not None:
            try:
                start = datetime.fromisoformat(profile.active_since)
                end = datetime.fromisoformat(t.timestamp)
            except ValueError:
                profile.active_since = None
                continue
            profile.active_since = None
            duration_s = (end - start).total_seconds()
            if duration_s > 0:
                _record_cycle(profile, duration_s)
                cycles += 1

    if fresh:
        profile_set.last_folded = fresh[-1].timestamp
    return cycles


def _record_cycle(profile: EntityProfile, duration_s: float) -> None:
    profile.cycle_count += 1
    profile.max_duration_s = max(profile.max_duration_s, duration_s)
    profile.duration_histogram[_bucket_index(duration_s)] += 1


def _bucket_index(duration_s: float) -> int:
    for i, edge in enumerate(BUCKET_EDGES_S):
        if duration_s <= edge:
            return i
    return len(BUCKET_EDGES_S)


def p95_duration_s(profile: EntityProfile) -> float:
    """95th-percentile cycle duration — upper bound of its bucket.

    The open-ended last bucket uses the exact observed max instead.
    """
    total = sum(profile.duration_histogram)
    if total == 0:
        return 0.0
    target = math.ceil(total * 0.95)
    cumulative = 0
    for i, count in enumerate(profile.duration_histogram):
        cumulative += count
        if cumulative >= target:
            if i < len(BUCKET_EDGES_S):
                return float(BUCKET_EDGES_S[i])
            return profile.max_duration_s
    return profile.max_duration_s
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_profiles.py -v`
Expected: 11 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests --fix && ruff format src tests
git add src/mylo/monitor/profiles.py tests/unit/test_profiles.py
git commit -m "feat(monitor): fold transitions into profile aggregates with watermark"
```

---

### Task 3: Confidence gates and duration thresholds

**Files:**
- Modify: `src/mylo/monitor/profiles.py` (append functions)
- Test: `tests/unit/test_profiles.py` (append tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_profiles.py` (merge names into the existing import):

```python
from mylo.monitor.profiles import (  # noqa: E402  (merge into the import at top)
    away_eligible,
    confidence,
    duration_eligible,
    duration_threshold_s,
    is_rarely_on_while_away,
)


def _confident_profile(**overrides: object) -> EntityProfile:
    p = EntityProfile(entity_id="light.kitchen", days_observed=20, cycle_count=20)
    p.duration_histogram[2] = 20  # twenty ~30min cycles → p95 = 1800s
    p.max_duration_s = 2100.0  # 35 min
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def test_duration_eligibility_gates() -> None:
    assert duration_eligible(_confident_profile())
    assert not duration_eligible(_confident_profile(days_observed=10))
    assert not duration_eligible(_confident_profile(cycle_count=5))


def test_away_eligibility_and_rarity() -> None:
    p = _confident_profile(away_samples=10, on_while_away_samples=1)
    assert away_eligible(p)
    assert is_rarely_on_while_away(p)  # 10% < 20%
    routine = _confident_profile(away_samples=10, on_while_away_samples=8)
    assert not is_rarely_on_while_away(routine)  # 80% — porch light
    assert not away_eligible(_confident_profile(away_samples=3))


def test_confidence_scales_with_days() -> None:
    assert confidence(_confident_profile(days_observed=21)) == 1.0
    assert abs(confidence(_confident_profile(days_observed=7)) - 7 / 21) < 1e-9


def test_duration_threshold_default_margins() -> None:
    p = _confident_profile()
    # max(2100 * 1.25, 1800 * 2.0) = max(2625, 3600) = 3600
    assert duration_threshold_s(p) == 3600.0


def test_duration_threshold_lock_uses_tight_margins() -> None:
    p = EntityProfile(entity_id="lock.front", days_observed=20, cycle_count=20)
    p.duration_histogram[2] = 20
    p.max_duration_s = 2100.0
    # max(2100 * 1.1, 1800 * 1.5) = max(2310, 2700) = 2700
    assert duration_threshold_s(p) == 2700.0


def test_duration_threshold_door_sensor_uses_tight_margins() -> None:
    p = EntityProfile(entity_id="binary_sensor.garage", days_observed=20, cycle_count=20)
    p.duration_histogram[2] = 20
    p.max_duration_s = 2100.0
    assert duration_threshold_s(p, device_class="garage_door") == 2700.0
    assert duration_threshold_s(p, device_class="motion") == 3600.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_profiles.py -v`
Expected: FAIL — `ImportError: cannot import name 'away_eligible'`

- [ ] **Step 3: Implement gates and thresholds**

Append to `src/mylo/monitor/profiles.py`:

```python
def duration_eligible(profile: EntityProfile) -> bool:
    """Quiet until confident: enough days AND enough cycles."""
    return (
        profile.days_observed >= MIN_DAYS_FOR_DURATION
        and profile.cycle_count >= MIN_CYCLES_FOR_DURATION
    )


def away_eligible(profile: EntityProfile) -> bool:
    return profile.away_samples >= MIN_AWAY_SAMPLES


def is_rarely_on_while_away(profile: EntityProfile) -> bool:
    """True when being on during away time is unusual for this entity."""
    if profile.away_samples == 0:
        return False
    return profile.on_while_away_samples / profile.away_samples < AWAY_RARE_FRACTION


def confidence(profile: EntityProfile) -> float:
    """0..1 — orders findings in the banner, never shown as a number."""
    return min(1.0, profile.days_observed / CONFIDENCE_FULL_DAYS)


def duration_threshold_s(profile: EntityProfile, *, device_class: str | None = None) -> float:
    """Seconds in the active state beyond which a finding fires.

    Must beat the all-time max with margin AND a multiple of the
    typical (p95) duration — whichever is larger governs.
    """
    domain = profile.entity_id.split(".", 1)[0]
    tight = domain == "lock" or (
        domain == "binary_sensor" and device_class in TIGHT_DEVICE_CLASSES
    )
    margin_max, margin_p95 = TIGHT_MARGINS if tight else DEFAULT_MARGINS
    return max(profile.max_duration_s * margin_max, p95_duration_s(profile) * margin_p95)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_profiles.py -v`
Expected: 17 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests --fix && ruff format src tests
git add src/mylo/monitor/profiles.py tests/unit/test_profiles.py
git commit -m "feat(monitor): confidence gates + learned duration thresholds"
```

---

### Task 4: Findings store (bounded pending_actions)

**Files:**
- Modify: `src/mylo/memory/schema.py` (PendingAction fields, new FindingCooldown, MemoryFile field)
- Create: `src/mylo/monitor/findings.py`
- Test: `tests/unit/test_findings.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_findings.py` (Apache header first):

```python
"""Tests for the bounded findings store that replaces append-forever
pending_actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mylo.memory.schema import PendingAction, empty_memory
from mylo.monitor.findings import (
    dismiss_all,
    dismiss_finding,
    expire_old,
    in_cooldown,
    migrate_legacy,
    resolve_stale,
    upsert_finding,
)

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)


def _upsert(memory, entity_id="light.kitchen", ftype="duration_anomaly", conf=0.9, now=NOW):
    return upsert_finding(
        memory,
        finding_id=f"{ftype}_{entity_id}",
        finding_type=ftype,
        entity_id=entity_id,
        title="t",
        message="m",
        confidence=conf,
        now=now,
    )


def test_upsert_is_keyed_not_appended() -> None:
    memory = empty_memory()
    assert _upsert(memory) is True
    assert _upsert(memory, now=NOW + timedelta(hours=1)) is False
    assert len(memory.pending_actions) == 1
    assert memory.pending_actions[0].last_seen == (NOW + timedelta(hours=1)).isoformat(
        timespec="seconds"
    )


def test_cap_evicts_lowest_confidence() -> None:
    memory = empty_memory()
    for i in range(5):
        _upsert(memory, entity_id=f"light.l{i}", conf=0.5 + i / 10)
    _upsert(memory, entity_id="light.high", conf=0.99)
    assert len(memory.pending_actions) == 5
    entity_ids = {pa.entity_id for pa in memory.pending_actions}
    assert "light.l0" not in entity_ids  # lowest confidence evicted
    assert "light.high" in entity_ids


def test_resolve_stale_deletes_cleared_findings() -> None:
    memory = empty_memory()
    _upsert(memory, entity_id="light.a")
    _upsert(memory, entity_id="light.b")
    removed = resolve_stale(memory, "duration_anomaly", {"light.b"})
    assert removed == 1
    assert [pa.entity_id for pa in memory.pending_actions] == ["light.b"]


def test_resolve_stale_only_touches_its_type() -> None:
    memory = empty_memory()
    _upsert(memory, entity_id="light.a", ftype="duration_anomaly")
    _upsert(memory, entity_id="light.a", ftype="while_away")
    resolve_stale(memory, "duration_anomaly", set())
    assert len(memory.pending_actions) == 1
    assert memory.pending_actions[0].type == "while_away"


def test_expire_old_uses_48h_ttl() -> None:
    memory = empty_memory()
    _upsert(memory, entity_id="light.old", now=NOW - timedelta(hours=49))
    _upsert(memory, entity_id="light.new", now=NOW - timedelta(hours=1))
    removed = expire_old(memory, NOW)
    assert removed == 1
    assert memory.pending_actions[0].entity_id == "light.new"


def test_dismiss_sets_cooldown_and_deletes() -> None:
    memory = empty_memory()
    _upsert(memory)
    assert dismiss_finding(memory, "duration_anomaly_light.kitchen", NOW) is True
    assert memory.pending_actions == []
    assert in_cooldown(memory, "duration_anomaly", "light.kitchen", NOW) is True
    assert (
        in_cooldown(memory, "duration_anomaly", "light.kitchen", NOW + timedelta(days=8))
        is False
    )
    assert in_cooldown(memory, "while_away", "light.kitchen", NOW) is False


def test_dismiss_all_cools_everything() -> None:
    memory = empty_memory()
    _upsert(memory, entity_id="light.a")
    _upsert(memory, entity_id="light.b", ftype="while_away")
    assert dismiss_all(memory, NOW) == 2
    assert memory.pending_actions == []
    assert in_cooldown(memory, "duration_anomaly", "light.a", NOW)
    assert in_cooldown(memory, "while_away", "light.b", NOW)


def test_migrate_legacy_drops_old_entries() -> None:
    memory = empty_memory()
    # Legacy entry: written before last_seen existed.
    memory.pending_actions.append(
        PendingAction(
            id="on_while_away_light.x",
            type="on_while_away",
            entity_id="light.x",
            title="t",
            message="m",
            detected_at="2026-05-01T00:00:00+00:00",
        )
    )
    _upsert(memory)  # new-style entry survives
    assert migrate_legacy(memory) == 1
    assert len(memory.pending_actions) == 1
    assert memory.pending_actions[0].type == "duration_anomaly"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_findings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mylo.monitor.findings'`

- [ ] **Step 3: Update the schema**

In `src/mylo/memory/schema.py`:

3a. Extend `PendingAction` (currently lines 252-268) — add two fields after `detected_at`:

```python
class PendingAction(BaseModel):
    """A monitor finding waiting to be shown in the catch-up banner.

    Lifecycle is managed by ``mylo.monitor.findings``: keyed by
    (type, entity_id), capped, auto-resolved when the condition
    clears, expired after 48h. Dismissing applies a cooldown.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str  # duration_anomaly, while_away, anomaly, unavailable, stale_automation
    entity_id: str
    title: str
    message: str
    detected_at: str  # ISO timestamp
    last_seen: str | None = None  # refreshed on every re-detection
    confidence: float = 1.0  # orders the banner; lowest evicted at cap
    resolved: bool = False  # legacy field — kept for migration detection
```

3b. Add a new model directly after `PendingAction`:

```python
class FindingCooldown(BaseModel):
    """A dismissed finding's snooze — detectors skip this
    (type, entity) pair until ``until``."""

    model_config = ConfigDict(extra="allow")

    type: str
    entity_id: str
    until: str  # ISO timestamp
```

3c. Add the field to `MemoryFile` after `pending_actions` (line 313):

```python
    finding_cooldowns: list[FindingCooldown] = Field(default_factory=list)
```

- [ ] **Step 4: Create the findings module**

Create `src/mylo/monitor/findings.py` (Apache header first):

```python
"""Findings store — the bounded, self-cleaning replacement for the
old append-forever pending_actions semantics.

Findings still live in ``MemoryFile.pending_actions`` (same field,
same catch-up banner plumbing) but with new lifecycle rules:

* Keyed by ``(type, entity_id)`` — re-detection refreshes, never duplicates.
* Auto-resolve — a successful sweep deletes findings it no longer sees.
* TTL — findings older than 48h are deleted regardless.
* Cap — at most 5 active findings; lowest confidence evicted.
* Dismiss — sets a 7-day per-(type, entity) cooldown, then deletes.

Legacy entries (written before ``last_seen`` existed) are dropped by
``migrate_legacy`` on the first sweep after upgrade.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mylo.logging_setup import get_logger
from mylo.memory.schema import FindingCooldown, MemoryFile, PendingAction

log = get_logger(__name__)

MAX_ACTIVE_FINDINGS = 5
FINDING_TTL_HOURS = 48
DISMISS_COOLDOWN_DAYS = 7


def upsert_finding(
    memory: MemoryFile,
    *,
    finding_id: str,
    finding_type: str,
    entity_id: str,
    title: str,
    message: str,
    confidence: float,
    now: datetime,
) -> bool:
    """Insert or refresh a finding. Returns True only when newly inserted."""
    ts = now.isoformat(timespec="seconds")
    for pa in memory.pending_actions:
        if pa.type == finding_type and pa.entity_id == entity_id:
            pa.title = title
            pa.message = message
            pa.last_seen = ts
            pa.confidence = confidence
            return False

    memory.pending_actions.append(
        PendingAction(
            id=finding_id,
            type=finding_type,
            entity_id=entity_id,
            title=title,
            message=message,
            detected_at=ts,
            last_seen=ts,
            confidence=confidence,
        )
    )
    while len(memory.pending_actions) > MAX_ACTIVE_FINDINGS:
        lowest = min(memory.pending_actions, key=lambda pa: pa.confidence)
        memory.pending_actions.remove(lowest)
    return True


def resolve_stale(memory: MemoryFile, finding_type: str, active_entity_ids: set[str]) -> int:
    """Delete findings of this type that the sweep no longer sees.

    Call only after a SUCCESSFUL check of that type — a failed sweep
    must not resolve anything. Returns the number removed.
    """
    before = len(memory.pending_actions)
    memory.pending_actions = [
        pa
        for pa in memory.pending_actions
        if pa.type != finding_type or pa.entity_id in active_entity_ids
    ]
    return before - len(memory.pending_actions)


def expire_old(memory: MemoryFile, now: datetime) -> int:
    """Delete findings older than the TTL. Returns the number removed."""
    cutoff = (now - timedelta(hours=FINDING_TTL_HOURS)).isoformat(timespec="seconds")
    before = len(memory.pending_actions)
    memory.pending_actions = [pa for pa in memory.pending_actions if pa.detected_at >= cutoff]
    return before - len(memory.pending_actions)


def in_cooldown(memory: MemoryFile, finding_type: str, entity_id: str, now: datetime) -> bool:
    ts = now.isoformat(timespec="seconds")
    return any(
        cd.type == finding_type and cd.entity_id == entity_id and cd.until > ts
        for cd in memory.finding_cooldowns
    )


def _set_cooldown(memory: MemoryFile, finding_type: str, entity_id: str, now: datetime) -> None:
    ts = now.isoformat(timespec="seconds")
    until = (now + timedelta(days=DISMISS_COOLDOWN_DAYS)).isoformat(timespec="seconds")
    # Prune expired cooldowns while we're here — keeps the list bounded.
    memory.finding_cooldowns = [cd for cd in memory.finding_cooldowns if cd.until > ts]
    for cd in memory.finding_cooldowns:
        if cd.type == finding_type and cd.entity_id == entity_id:
            cd.until = until
            return
    memory.finding_cooldowns.append(
        FindingCooldown(type=finding_type, entity_id=entity_id, until=until)
    )


def dismiss_finding(memory: MemoryFile, finding_id: str, now: datetime) -> bool:
    """Dismiss one finding: cooldown its (type, entity), then delete."""
    for pa in memory.pending_actions:
        if pa.id == finding_id:
            _set_cooldown(memory, pa.type, pa.entity_id, now)
            memory.pending_actions.remove(pa)
            return True
    return False


def dismiss_all(memory: MemoryFile, now: datetime) -> int:
    """Dismiss everything currently in the store."""
    count = len(memory.pending_actions)
    for pa in memory.pending_actions:
        _set_cooldown(memory, pa.type, pa.entity_id, now)
    memory.pending_actions = []
    return count


def migrate_legacy(memory: MemoryFile) -> int:
    """Drop pre-rework entries: anything resolved or missing last_seen."""
    before = len(memory.pending_actions)
    memory.pending_actions = [
        pa for pa in memory.pending_actions if pa.last_seen is not None and not pa.resolved
    ]
    dropped = before - len(memory.pending_actions)
    if dropped:
        log.info("findings.migrated_legacy", dropped=dropped)
    return dropped
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_findings.py tests/unit/test_memory.py -v`
Expected: all pass (test_memory.py confirms the schema change is backward compatible)

- [ ] **Step 6: Lint and commit**

```bash
ruff check src tests --fix && ruff format src tests
git add src/mylo/memory/schema.py src/mylo/monitor/findings.py tests/unit/test_findings.py
git commit -m "feat(monitor): bounded findings store with TTL, cap, and dismiss cooldowns"
```

---

### Task 5: Harden z-score anomalies (3.5σ + persistence)

**Files:**
- Modify: `src/mylo/monitor/anomaly.py`
- Test: `tests/unit/test_monitor.py` (update existing anomaly tests + add persistence tests)

- [ ] **Step 1: Update/add tests**

In `tests/unit/test_monitor.py`, the existing anomaly tests (`test_anomaly_detects_spike` at line 272 and the others that follow) assume one check is enough. Update `test_anomaly_detects_spike` to call twice and import the new reset:

```python
from mylo.monitor.anomaly import check_anomalies, reset_state as reset_anomaly_state


async def test_anomaly_detects_persistent_spike() -> None:
    reset_anomaly_state()
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=[
            {
                "entity_id": "sensor.power",
                "state": "450.0",
                "attributes": {
                    "friendly_name": "Power usage",
                    "unit_of_measurement": "W",
                },
            },
        ]
    )
    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
        ]
    )

    # First check: anomalous but not persistent yet — no finding.
    first = await check_anomalies(ws_client=ws, baselines=baselines)
    assert first == []

    # Second consecutive check: now it fires.
    second = await check_anomalies(ws_client=ws, baselines=baselines)
    assert len(second) == 1
    assert second[0]["z_score"] == 5.0
    assert second[0]["severity"] == "high"


async def test_anomaly_blip_resets_streak() -> None:
    reset_anomaly_state()
    ws = AsyncMock()
    baselines = Baselines(
        entities=[
            EntityBaseline(entity="sensor.power", metric="mean", avg=200.0, stddev=50.0),
        ]
    )
    spike = [{"entity_id": "sensor.power", "state": "450.0", "attributes": {}}]
    normal = [{"entity_id": "sensor.power", "state": "205.0", "attributes": {}}]

    ws.send_command = AsyncMock(return_value=spike)
    assert await check_anomalies(ws_client=ws, baselines=baselines) == []
    ws.send_command = AsyncMock(return_value=normal)
    assert await check_anomalies(ws_client=ws, baselines=baselines) == []
    ws.send_command = AsyncMock(return_value=spike)
    # Streak was reset — still only the first anomalous check.
    assert await check_anomalies(ws_client=ws, baselines=baselines) == []
```

Delete or rewrite the old `test_anomaly_detects_spike` (replaced by `test_anomaly_detects_persistent_spike`). Check the remaining anomaly tests in the file (around lines 300-380): any test asserting a finding from a single check must either call `check_anomalies` twice or assert `== []`; threshold-boundary tests must use the new 3.5 default (e.g. a z of 3.0 that previously fired at 2.5 no longer does). Add `reset_anomaly_state()` at the top of every anomaly test so streaks don't leak between tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_monitor.py -v -k anomaly`
Expected: FAIL — `ImportError: cannot import name 'reset_state' from 'mylo.monitor.anomaly'`

- [ ] **Step 3: Implement persistence in anomaly.py**

In `src/mylo/monitor/anomaly.py`:

3a. Replace `Z_THRESHOLD = 2.5` (line 41) and add persistence state:

```python
Z_THRESHOLD = 3.5

# An anomaly must persist for this many consecutive hourly checks
# before a finding is emitted — kills one-hour blips.
PERSISTENCE_CHECKS = 2

# Per-entity count of consecutive anomalous checks.
_streaks: dict[str, int] = {}
```

3b. Update the docstring paragraph about 2.5 sd (lines 21-24) to describe 3.5σ + 2-check persistence.

3c. In the loop inside `check_anomalies`, replace the two early-continue checks (lines 90-95):

```python
        if abs_z < threshold or abs(value - baseline.avg) < MIN_ABSOLUTE_CHANGE:
            _streaks.pop(baseline.entity, None)
            continue

        streak = _streaks.get(baseline.entity, 0) + 1
        _streaks[baseline.entity] = streak
        if streak < PERSISTENCE_CHECKS:
            continue
```

3d. Update the severity mapping (lines 102-108) for the higher base threshold:

```python
        severity: str
        if abs_z >= 4.5:
            severity = "high"
        elif abs_z >= 4.0:
            severity = "normal"
        else:
            severity = "low"
```

3e. Add at the end of the file (mirrors `hourly.reset_state`):

```python
def reset_state() -> None:
    """Clear persistence streaks — used by tests."""
    _streaks.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_monitor.py -v`
Expected: all pass

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests --fix && ruff format src tests
git add src/mylo/monitor/anomaly.py tests/unit/test_monitor.py
git commit -m "feat(anomaly): raise threshold to 3.5σ and require 2-check persistence"
```

---

### Task 6: Learned detectors (duration + while-away)

**Files:**
- Create: `src/mylo/monitor/detectors.py`
- Modify: `src/mylo/monitor/suggestions.py` (add `confidence` field to SuggestionAction)
- Test: `tests/unit/test_detectors.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_detectors.py` (Apache header first):

```python
"""Tests for profile-gated learned detectors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from mylo.memory.schema import empty_memory
from mylo.monitor.detectors import run_learned_checks
from mylo.monitor.profiles import EntityProfile, ProfileSet


def _profile(entity_id: str, *, max_s: float = 2100.0, bucket: int = 2, **overrides: object):
    p = EntityProfile(entity_id=entity_id, days_observed=20, cycle_count=20)
    p.duration_histogram[bucket] = 20
    p.max_duration_s = max_s
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def _state(entity_id: str, state: str, *, hours_ago: float = 0.0, attributes=None):
    changed = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": changed,
        "attributes": attributes or {},
    }


def _ws(states: list[dict]) -> AsyncMock:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value=states)
    return ws


async def test_duration_anomaly_fires_beyond_learned_threshold() -> None:
    # Typical use ~30min (p95=1800s), max 35min → threshold 3600s. On 4h.
    profiles = ProfileSet(entities={"light.kitchen": _profile("light.kitchen")})
    ws = _ws([_state("light.kitchen", "on", hours_ago=4.0)])

    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert len(actions) == 1
    assert actions[0].type == "duration_anomaly"
    assert actions[0].entity_id == "light.kitchen"
    assert "longest" in actions[0].message


async def test_duration_silent_within_learned_norms() -> None:
    # Routinely on ~8h (p95 bucket ≤8h → 28800s); max 8.2h.
    # Threshold = max(29520 * 1.25, 28800 * 2) = 57600s = 16h. On 9h → silent.
    profiles = ProfileSet(
        entities={"light.office": _profile("light.office", max_s=29520.0, bucket=6)}
    )
    ws = _ws([_state("light.office", "on", hours_ago=9.0)])

    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert actions == []


async def test_duration_silent_when_not_confident() -> None:
    profiles = ProfileSet(
        entities={"light.new": _profile("light.new", days_observed=5)}
    )
    ws = _ws([_state("light.new", "on", hours_ago=12.0)])

    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert actions == []


async def test_duration_silent_with_no_profile() -> None:
    ws = _ws([_state("light.unknown", "on", hours_ago=12.0)])
    actions = await run_learned_checks(
        ws_client=ws, memory=empty_memory(), profiles=ProfileSet()
    )
    assert actions == []


async def test_while_away_fires_only_when_rare() -> None:
    rare = _profile("light.closet", away_samples=10, on_while_away_samples=0)
    routine = _profile("light.porch", away_samples=10, on_while_away_samples=9)
    profiles = ProfileSet(entities={"light.closet": rare, "light.porch": routine})
    ws = _ws(
        [
            _state("person.max", "not_home"),
            _state("light.closet", "on", hours_ago=0.2),
            _state("light.porch", "on", hours_ago=0.2),
        ]
    )

    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    while_away = [a for a in actions if a.type == "while_away"]
    assert [a.entity_id for a in while_away] == ["light.closet"]


async def test_while_away_silent_when_someone_home() -> None:
    rare = _profile("light.closet", away_samples=10, on_while_away_samples=0)
    profiles = ProfileSet(entities={"light.closet": rare})
    ws = _ws(
        [
            _state("person.max", "home"),
            _state("light.closet", "on", hours_ago=0.2),
        ]
    )
    actions = await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert [a for a in actions if a.type == "while_away"] == []


async def test_away_samples_recorded_while_away() -> None:
    profiles = ProfileSet()
    ws = _ws(
        [
            _state("person.max", "not_home"),
            _state("light.kitchen", "on", hours_ago=0.2),
            _state("light.hall", "off"),
        ]
    )
    await run_learned_checks(ws_client=ws, memory=empty_memory(), profiles=profiles)
    assert profiles.entities["light.kitchen"].away_samples == 1
    assert profiles.entities["light.kitchen"].on_while_away_samples == 1
    assert profiles.entities["light.hall"].away_samples == 1
    assert profiles.entities["light.hall"].on_while_away_samples == 0


async def test_suppression_silences_detector() -> None:
    from mylo.memory.schema import NotificationSuppression

    memory = empty_memory()
    memory.notification_suppressions.append(
        NotificationSuppression(type="duration_anomaly", entity="light.kitchen")
    )
    profiles = ProfileSet(entities={"light.kitchen": _profile("light.kitchen")})
    ws = _ws([_state("light.kitchen", "on", hours_ago=4.0)])

    actions = await run_learned_checks(ws_client=ws, memory=memory, profiles=profiles)
    assert actions == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_detectors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mylo.monitor.detectors'`

- [ ] **Step 3: Add confidence to SuggestionAction**

In `src/mylo/monitor/suggestions.py`, add one field to the `SuggestionAction` dataclass (after `offer_automation`, line 78):

```python
    # Profile confidence (0..1) — orders findings in the banner.
    confidence: float = 1.0
```

- [ ] **Step 4: Create the detectors module**

Create `src/mylo/monitor/detectors.py` (Apache header first):

```python
"""Profile-gated detectors — the learned replacement for the old
fixed-threshold suggestion rules.

Every check here must pass the entity's own learned profile before
it can fire:

* **duration_anomaly** — an entity has been in its active state
  longer than its learned threshold (its own all-time max with a
  margin, and a multiple of its typical p95 duration).
* **while_away** — an entity is on while everyone is away AND its
  profile says that's rare for this entity.

Entities without enough history stay silent — quiet until confident.
This module also does the learning side of the while-away check:
each sweep that runs during away time records one sample per
light/switch into the profiles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mylo.ha.states import get_all_states
from mylo.ha.ws_client import HaWsClient
from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile
from mylo.monitor.profiles import (
    ACTIVE_STATES,
    EntityProfile,
    ProfileSet,
    away_eligible,
    confidence,
    duration_eligible,
    duration_threshold_s,
    is_rarely_on_while_away,
)
from mylo.monitor.suggestions import (
    SuggestionAction,
    _should_offer_automation,
    _should_skip,
)

log = get_logger(__name__)

# Domains sampled and checked for while-away behavior.
AWAY_DOMAINS = ("light", "switch")

_TURN_OFF_SERVICES: dict[str, str] = {
    "light": "light.turn_off",
    "switch": "switch.turn_off",
    "fan": "fan.turn_off",
    "media_player": "media_player.turn_off",
    "lock": "lock.lock",
    "cover": "cover.close_cover",
}

# How to phrase the active state in messages ("on" for most domains).
_STATE_VERBS: dict[str, str] = {
    "lock": "unlocked",
    "cover": "open",
    "binary_sensor": "open",
}


async def run_learned_checks(
    *,
    ws_client: HaWsClient,
    memory: MemoryFile,
    profiles: ProfileSet,
) -> list[SuggestionAction]:
    """Run duration + while-away checks against learned profiles.

    Mutates ``profiles`` (away samples) — the caller is responsible
    for saving the profile set afterwards.
    """
    states = await get_all_states(ws_client)
    now = datetime.now(UTC)
    actions: list[SuggestionAction] = []

    person_states = {
        eid: s.get("state", "") for eid, s in states.items() if eid.startswith("person.")
    }
    anyone_home = any(s == "home" for s in person_states.values()) if person_states else True
    away = bool(person_states) and not anyone_home

    if away:
        _record_away_samples(profiles, states, now)

    # 1. Duration anomalies.
    for entity_id, state in states.items():
        domain = entity_id.split(".", 1)[0]
        active = ACTIVE_STATES.get(domain)
        if active is None or state.get("state") != active:
            continue
        profile = profiles.entities.get(entity_id)
        if profile is None or not duration_eligible(profile):
            continue

        duration_s = _seconds_in_state(state, now)
        if duration_s is None:
            continue
        device_class = state.get("attributes", {}).get("device_class")
        threshold = duration_threshold_s(profile, device_class=device_class)
        if threshold <= 0 or duration_s <= threshold:
            continue

        if memory.is_notification_suppressed("duration_anomaly", entity_id):
            continue
        sid = f"duration_anomaly_{entity_id}"
        if _should_skip(sid, memory):
            continue

        friendly = state.get("attributes", {}).get("friendly_name", entity_id)
        verb = _STATE_VERBS.get(domain, "on")
        actions.append(
            SuggestionAction(
                suggestion_id=sid,
                type="duration_anomaly",
                entity_id=entity_id,
                title=f"{friendly} {verb} unusually long",
                message=(
                    f"{friendly} has been {verb} for {_fmt_duration(duration_s)} — "
                    f"the longest you've left it before is "
                    f"{_fmt_duration(profile.max_duration_s)}."
                ),
                accept_service=_TURN_OFF_SERVICES.get(domain),
                accept_target=(
                    {"entity_id": entity_id} if domain in _TURN_OFF_SERVICES else {}
                ),
                confidence=confidence(profile),
                offer_automation=_should_offer_automation(sid, memory),
            )
        )

    # 2. On while away — only when that's rare for this entity.
    if away:
        for entity_id, state in states.items():
            domain = entity_id.split(".", 1)[0]
            if domain not in AWAY_DOMAINS or state.get("state") != "on":
                continue
            profile = profiles.entities.get(entity_id)
            if profile is None or not away_eligible(profile):
                continue
            if not is_rarely_on_while_away(profile):
                continue
            if memory.is_notification_suppressed("while_away", entity_id):
                continue
            sid = f"while_away_{entity_id}"
            if _should_skip(sid, memory):
                continue

            friendly = state.get("attributes", {}).get("friendly_name", entity_id)
            actions.append(
                SuggestionAction(
                    suggestion_id=sid,
                    type="while_away",
                    entity_id=entity_id,
                    title=f"{friendly} is on while you're away",
                    message=(
                        f"{friendly} is on and nobody is home — you don't usually "
                        f"leave it on when you're out. Want me to turn it off?"
                    ),
                    accept_service=f"{domain}.turn_off",
                    accept_target={"entity_id": entity_id},
                    confidence=confidence(profile),
                    offer_automation=_should_offer_automation(sid, memory),
                )
            )

    return actions


def _record_away_samples(
    profiles: ProfileSet,
    states: dict[str, dict[str, Any]],
    now: datetime,
) -> None:
    """One hourly observation of away-time behavior per entity."""
    for entity_id, state in states.items():
        domain = entity_id.split(".", 1)[0]
        if domain not in AWAY_DOMAINS:
            continue
        s = state.get("state", "")
        if s in ("unavailable", "unknown"):
            continue
        profile = profiles.entities.get(entity_id)
        if profile is None:
            profile = EntityProfile(
                entity_id=entity_id, first_seen=now.isoformat(timespec="seconds")
            )
            profiles.entities[entity_id] = profile
        profile.away_samples += 1
        if s == "on":
            profile.on_while_away_samples += 1


def _seconds_in_state(state: dict[str, Any], now: datetime) -> float | None:
    last_changed = state.get("last_changed")
    if not last_changed:
        return None
    try:
        changed_dt = datetime.fromisoformat(str(last_changed).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (now - changed_dt).total_seconds()


def _fmt_duration(seconds: float) -> str:
    hours = seconds / 3600
    if hours >= 1:
        text = f"{hours:.1f}".rstrip("0").rstrip(".")
        return f"{text}h"
    return f"{int(seconds / 60)}m"
```

Note: `get_all_states` returns a dict keyed by entity_id (the AsyncMock in tests returns a list that `get_all_states` converts — same pattern as `tests/unit/test_monitor.py:202-212`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_detectors.py -v`
Expected: 8 passed

- [ ] **Step 6: Lint and commit**

```bash
ruff check src tests --fix && ruff format src tests
git add src/mylo/monitor/detectors.py src/mylo/monitor/suggestions.py tests/unit/test_detectors.py
git commit -m "feat(monitor): profile-gated duration + while-away detectors"
```

---

### Task 7: Delete the fixed-threshold detectors

**Files:**
- Modify: `src/mylo/monitor/suggestions.py` (delete `run_suggestions` + threshold constants)
- Modify: `src/mylo/monitor/hourly.py` (delete while-away section, add `current_unavailable()`, add `entity_id` to stale findings)
- Modify: `src/mylo/context/assembler.py:228-244` (`_proposal_description` for new types)
- Modify: `src/mylo/data/system_prompt.txt:78-84` (suppression type rename)

- [ ] **Step 1: Strip suggestions.py**

Delete from `src/mylo/monitor/suggestions.py`:
- `UNLOCK_THRESHOLD_MINUTES` and `DEVICE_ON_THRESHOLD_HOURS` constants (lines 50-54)
- The entire `run_suggestions` function (lines 81-215)
- Now-unused imports: `timedelta` from datetime, `get_all_states`, `HaWsClient` (keep `datetime`/`UTC` — still used by `record_suggestion`)

Update the module docstring: the engine no longer generates suggestions itself; it keeps `SuggestionAction` (the shared shape detectors emit), outcome tracking (`record_suggestion`, `record_outcome`), rejection-based silencing (`_should_skip`), and automation proposals (`_should_offer_automation`). Detection now lives in `mylo.monitor.detectors`, gated by learned profiles.

- [ ] **Step 2: Trim hourly.py**

In `src/mylo/monitor/hourly.py`:

2a. Delete section 3 — "Presence-aware: lights/switches on while nobody is home" (lines 156-193), and remove item 3 from the module docstring (replace with a note that presence-aware checks live in `mylo.monitor.detectors`).

2b. Give stale-automation findings an `entity_id` (the finding dict at lines 142-152) — add one key:

```python
                        "entity_id": entity_id,
```

Also add `"entity_id": ""` to the unavailable finding dict (lines 113-120) so both shapes are uniform.

2c. Add an accessor after `run_hourly_check` (the scheduler uses this to auto-resolve the aggregated unavailable finding):

```python
def current_unavailable() -> set[str]:
    """Entities unavailable as of the last sweep (post-sweep snapshot)."""
    return set(_previously_unavailable)
```

- [ ] **Step 3: Update assembler proposal descriptions**

In `src/mylo/context/assembler.py`, replace the body of `_proposal_description` (lines 228-244):

```python
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
```

(`on_while_away` stays as a legacy alias so proposals from already-tracked suggestions still render; `unlocked_too_long`/`device_running_long` fall through to the generic line.)

- [ ] **Step 4: Update system_prompt.txt**

In `src/mylo/data/system_prompt.txt` (lines ~78-84), the infrastructure-devices paragraph references `type="on_while_away"`. Change to:

```
- When users say entities like network switches, cameras, servers,
  or other infrastructure are "always on" and should be ignored,
  use manage_notification_filters to add a suppression for each
  entity with type="while_away" (and type="duration_anomaly" if
  they also don't want long-running alerts for it). This prevents
  those entities from triggering "on while away" findings. Batch
  them in one call if the user lists multiple entities.
```

- [ ] **Step 5: Verify nothing still references the deleted code**

Run: `grep -rn "run_suggestions\|UNLOCK_THRESHOLD\|DEVICE_ON_THRESHOLD" src/ tests/`
Expected: only the import in `src/mylo/monitor/scheduler.py` (fixed in Task 8 — leave it broken for one commit is NOT acceptable, so do Step 6 check first).

**Important:** `scheduler.py:279` imports `run_suggestions`. To keep every commit green, apply the minimal scheduler stub now: in `_hourly_job`, delete section 3 (lines 272-317, the whole `try` block) and its now-unused imports. Task 8 rebuilds the job properly.

Run: `python -m pytest tests/unit/ -v`
Expected: all pass

- [ ] **Step 6: Lint and commit**

```bash
ruff check src tests --fix && ruff format src tests
git add src/mylo/monitor/suggestions.py src/mylo/monitor/hourly.py src/mylo/monitor/scheduler.py src/mylo/context/assembler.py src/mylo/data/system_prompt.txt
git commit -m "feat(monitor): remove fixed-threshold detectors in favor of learned norms"
```

---

### Task 8: Rewire the scheduler

**Files:**
- Modify: `src/mylo/monitor/scheduler.py` (`_hourly_job` rewrite + nightly fold step)

No new unit tests for the job glue itself — every piece it composes is unit-tested (profiles, findings, detectors, anomaly, hourly), and the existing scheduler tests only verify job registration, which is unchanged. Behavior is verified on the real HA instance.

**Concurrency guard:** the nightly fold and the hourly learned-check both do load→mutate→save on profiles.json, and the hourly holds its loaded `ProfileSet` across an `await` — a lost-update window on the shared event loop. Add a module-level lock to `scheduler.py`:

```python
import asyncio

# Serializes profiles.json load-modify-save between nightly fold and
# hourly learned checks — the hourly holds its ProfileSet across an
# await, so an unguarded nightly fold could be silently overwritten.
_profiles_lock = asyncio.Lock()
```

Wrap the nightly step-4 body (load→fold→save) and the hourly step-3 profile section (load→run_learned_checks→save) in `async with _profiles_lock:`.

- [ ] **Step 1: Add the nightly profile fold**

In `_nightly_job`, after step 3 (behavioral patterns, ends line 209) and before `log.info("nightly.finished")`, add:

```python
    # 4. Fold transitions into learned profiles.
    try:
        from mylo.monitor.profiles import ProfileStore, fold_transitions
        from mylo.monitor.transitions import RETENTION_DAYS, TransitionLogger

        raw_logger = app.get(AppKeys.TRANSITIONS)
        transition_logger = raw_logger if isinstance(raw_logger, TransitionLogger) else None
        if transition_logger is not None:
            profile_store = ProfileStore(config.mylo_data_dir)
            profile_set = profile_store.load()
            recent = transition_logger.read_recent(hours=RETENTION_DAYS * 24)
            folded = fold_transitions(profile_set, recent)
            profile_store.save(profile_set)
            log.info(
                "nightly.profiles_folded",
                cycles=folded,
                entities=len(profile_set.entities),
            )
    except Exception:
        log.exception("nightly.profiles_failed")
```

- [ ] **Step 2: Rewrite `_hourly_job`**

Replace the entire `_hourly_job` function with:

```python
async def _hourly_job(app: web.Application) -> None:
    """Hourly sweep: availability, anomalies, learned checks.

    All findings flow into the bounded findings store (surfaced in
    the catch-up banner). Availability and anomaly findings also
    notify — but only when newly detected, never on refresh. Memory
    is saved once at the end. A failed check never auto-resolves its
    findings (resolve calls live inside each check's try block).
    """
    from datetime import UTC, datetime

    from mylo.monitor import findings as findings_store
    from mylo.monitor.anomaly import check_anomalies
    from mylo.monitor.detectors import run_learned_checks
    from mylo.monitor.hourly import current_unavailable, run_hourly_check
    from mylo.monitor.notifier import Notifier
    from mylo.monitor.profiles import ProfileStore
    from mylo.monitor.suggestions import record_suggestion
    from mylo.server.app import AppKeys

    config = app[AppKeys.CONFIG]
    ws_client = app[AppKeys.HA_CLIENT]
    registries = app.get(AppKeys.REGISTRIES)
    store = app[AppKeys.MEMORY]
    memory = store.current()
    notifier = Notifier(ws_client=ws_client, config=config, memory=memory)
    now = datetime.now(UTC)

    log.info("hourly.started")

    dirty = findings_store.migrate_legacy(memory) > 0

    # 1. Availability sweep (notifies + findings store).
    try:
        findings = await run_hourly_check(ws_client=ws_client, registries=registries)
        stale_ids: set[str] = set()
        for finding in findings:
            ntype = "unavailable" if finding["id"] == "unavailable" else "stale_automation"
            entity_id = finding.get("entity_id", "")
            if ntype == "stale_automation":
                stale_ids.add(entity_id)
            if findings_store.in_cooldown(memory, ntype, entity_id, now):
                continue
            is_new = findings_store.upsert_finding(
                memory,
                finding_id=f"mylo_hourly_{finding['id']}",
                finding_type=ntype,
                entity_id=entity_id,
                title=finding["title"],
                message=finding["message"],
                confidence=1.0,
                now=now,
            )
            dirty = True
            if is_new:
                await notifier.send(
                    title=finding["title"],
                    message=finding["message"],
                    notification_id=f"mylo_hourly_{finding['id']}",
                    severity=finding.get("severity", "normal"),
                    notification_type=ntype,
                )
        dirty |= findings_store.resolve_stale(memory, "stale_automation", stale_ids) > 0
        if not current_unavailable():
            dirty |= findings_store.resolve_stale(memory, "unavailable", set()) > 0
        if findings:
            log.info("hourly.findings", count=len(findings))
    except Exception:
        log.exception("hourly.check_failed")

    # 2. Anomaly check (only if baselines exist).
    try:
        if memory.baselines.entities:
            anomalies = await check_anomalies(ws_client=ws_client, baselines=memory.baselines)
            active: set[str] = set()
            for anomaly in anomalies:
                entity_id = anomaly.get("entity_id", "")
                active.add(entity_id)
                if findings_store.in_cooldown(memory, "anomaly", entity_id, now):
                    continue
                is_new = findings_store.upsert_finding(
                    memory,
                    finding_id=f"mylo_anomaly_{anomaly['id']}",
                    finding_type="anomaly",
                    entity_id=entity_id,
                    title=anomaly["title"],
                    message=anomaly["message"],
                    confidence=1.0,
                    now=now,
                )
                dirty = True
                if is_new:
                    await notifier.send(
                        title=anomaly["title"],
                        message=anomaly["message"],
                        notification_id=f"mylo_anomaly_{anomaly['id']}",
                        severity=anomaly.get("severity", "normal"),
                        notification_type="anomaly",
                        entity_id=entity_id,
                    )
            dirty |= findings_store.resolve_stale(memory, "anomaly", active) > 0
            if anomalies:
                log.info("hourly.anomalies", count=len(anomalies))
    except Exception:
        log.exception("hourly.anomaly_failed")

    # 3. Learned checks (banner only — no HA notifications).
    try:
        profile_store = ProfileStore(config.mylo_data_dir)
        profile_set = profile_store.load()
        actions = await run_learned_checks(
            ws_client=ws_client, memory=memory, profiles=profile_set
        )
        profile_store.save(profile_set)  # away samples updated during the check

        active_by_type: dict[str, set[str]] = {
            "duration_anomaly": set(),
            "while_away": set(),
        }
        for action in actions:
            active_by_type.setdefault(action.type, set()).add(action.entity_id)
            if findings_store.in_cooldown(memory, action.type, action.entity_id, now):
                continue
            record_suggestion(
                memory, action.suggestion_id, action.type, action.entity_id, action.message
            )
            findings_store.upsert_finding(
                memory,
                finding_id=action.suggestion_id,
                finding_type=action.type,
                entity_id=action.entity_id,
                title=action.title,
                message=action.message,
                confidence=action.confidence,
                now=now,
            )
            dirty = True
        for ftype, keys in active_by_type.items():
            dirty |= findings_store.resolve_stale(memory, ftype, keys) > 0
        if actions:
            log.info("hourly.learned_findings", count=len(actions))
    except Exception:
        log.exception("hourly.learned_failed")

    dirty |= findings_store.expire_old(memory, now) > 0
    if dirty:
        await store.save(memory, note="hourly: findings updated")

    log.info("hourly.finished")
```

Also update the module docstring's hourly bullet (lines 23-25) to mention learned checks and the findings store.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all pass

- [ ] **Step 4: Lint and commit**

```bash
ruff check src tests --fix && ruff format src tests
git add src/mylo/monitor/scheduler.py
git commit -m "feat(monitor): route all hourly checks through the bounded findings store"
```

---

### Task 9: API — dismiss endpoints + sorted catchup

**Files:**
- Modify: `src/mylo/server/routes_memory.py` (rework clear, add dismiss)
- Modify: `src/mylo/server/routes_chat.py:138-153` (sort by confidence)

- [ ] **Step 1: Rework the clear handler and add single dismiss**

In `src/mylo/server/routes_memory.py`:

1a. Register the new route next to the existing one (line 52):

```python
    app.router.add_post("/api/pending-actions/dismiss", _handle_dismiss_pending_action)
```

1b. Replace `_handle_clear_pending_actions` (lines 440-460):

```python
async def _handle_clear_pending_actions(request: web.Request) -> web.Response:
    """Dismiss all findings.

    Applies the per-(type, entity) cooldown to each so detectors
    don't re-surface the same items next sweep, then deletes them.
    """
    from datetime import UTC, datetime

    from mylo.monitor import findings as findings_store
    from mylo.server.app import AppKeys

    store = request.app[AppKeys.MEMORY]
    memory = store.current()

    cleared = findings_store.dismiss_all(memory, datetime.now(UTC))
    if cleared:
        await store.save(memory, note=f"dismissed {cleared} findings")

    return web.json_response({"ok": True, "cleared": cleared})


async def _handle_dismiss_pending_action(request: web.Request) -> web.Response:
    """Dismiss one finding by id — cooldown its (type, entity), delete it."""
    from datetime import UTC, datetime

    from mylo.monitor import findings as findings_store
    from mylo.server.app import AppKeys

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    finding_id = str(body.get("id", "")).strip()
    if not finding_id:
        return web.json_response({"ok": False, "error": "missing_id"}, status=400)

    store = request.app[AppKeys.MEMORY]
    memory = store.current()

    dismissed = findings_store.dismiss_finding(memory, finding_id, datetime.now(UTC))
    if dismissed:
        await store.save(memory, note=f"dismissed finding {finding_id}")
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "not_found"}, status=404)
```

- [ ] **Step 2: Sort catchup findings by confidence**

In `src/mylo/server/routes_chat.py`, replace the pending-actions loop (lines 139-153):

```python
    if memory_store is not None:
        mem = memory_store.current()
        unresolved = [pa for pa in mem.pending_actions if not pa.resolved]
        unresolved.sort(key=lambda pa: pa.confidence, reverse=True)
        for pa in unresolved:
            lines.append(pa.message)
            pending_actions.append(
                {
                    "id": pa.id,
                    "type": pa.type,
                    "entity_id": pa.entity_id,
                    "title": pa.title,
                    "message": pa.message,
                    "detected_at": pa.detected_at,
                }
            )
```

(The store itself caps at 5, so no slicing needed here.)

- [ ] **Step 3: Run the suite, lint, commit**

```bash
python -m pytest tests/ -v
ruff check src tests --fix && ruff format src tests
git add src/mylo/server/routes_memory.py src/mylo/server/routes_chat.py
git commit -m "feat(api): per-finding dismiss with cooldown + confidence-ordered catchup"
```

---

### Task 10: UI — per-item dismiss in the catch-up banner

**Files:**
- Modify: `ui/src/api.ts` (add `dismissPendingAction`)
- Modify: `ui/src/components/CatchupBanner.tsx`

Do NOT run `npm run build` — the user verifies via HA rebuild.

- [ ] **Step 1: Add the API helper**

In `ui/src/api.ts`, after `clearPendingActions` (line 203):

```typescript
export async function dismissPendingAction(id: string): Promise<void> {
  await fetch(apiUrl("api/pending-actions/dismiss"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
}
```

- [ ] **Step 2: Per-item dismiss in CatchupBanner**

Replace the body of `ui/src/components/CatchupBanner.tsx` (keep the license header):

```tsx
import { useState } from "react";
import {
  clearPendingActions,
  dismissPendingAction,
  type CatchupData,
} from "../api";

interface Props {
  data: CatchupData;
  onDismiss: () => void;
}

// Injected as a divider between old messages and the input area.
// Not a message from Mylo — a lightweight status block that gives
// context without pretending the old conversation didn't happen.
// Findings from the monitor show here with accent styling; each can
// be dismissed individually (7-day cooldown server-side) or all at
// once.
export function CatchupBanner({ data, onDismiss }: Props) {
  const [dismissedIds, setDismissedIds] = useState<string[]>([]);

  if (!data.show_banner) return null;

  const pending = (data.pending_actions ?? []).filter(
    (pa) => !dismissedIds.includes(pa.id),
  );
  const hasPending = pending.length > 0;
  const regularLines = (data.lines || []).filter(
    (line) => !data.pending_actions?.some((pa) => pa.message === line),
  );

  const handleDismissAll = async () => {
    if (hasPending) {
      await clearPendingActions();
    }
    onDismiss();
  };

  const handleDismissItem = (id: string) => {
    setDismissedIds((prev) => [...prev, id]);
    void dismissPendingAction(id);
  };

  return (
    <div className="my-4">
      <div
        className="flex items-center gap-3 text-center"
        style={{ color: "var(--color-text-dim)" }}
      >
        <div
          className="flex-1 h-px"
          style={{ backgroundColor: "var(--color-border)" }}
        />
        <span className="font-mono text-[9px] uppercase tracking-label shrink-0">
          {data.gap_label}
        </span>
        <div
          className="flex-1 h-px"
          style={{ backgroundColor: "var(--color-border)" }}
        />
      </div>
      <div
        className="mt-3 rounded border px-4 py-3"
        style={{
          borderColor: hasPending
            ? "var(--color-border-accent)"
            : "var(--color-border)",
          backgroundColor: "var(--color-surface)",
        }}
      >
        <div
          className="font-mono text-[10px] uppercase tracking-label mb-2"
          style={{ color: "var(--color-text-dim)" }}
        >
          Since we last talked
        </div>

        {/* Regular activity lines */}
        {regularLines.length > 0 ? (
          <ul className="space-y-1 mb-2">
            {regularLines.map((line, i) => (
              <li
                key={i}
                className="flex items-start gap-2 font-sans text-[12.5px]"
                style={{ color: "var(--color-text-muted)" }}
              >
                <span
                  className="mt-1.5 shrink-0 h-[4px] w-[4px] rounded-full"
                  style={{ backgroundColor: "var(--color-text-muted)" }}
                />
                {line}
              </li>
            ))}
          </ul>
        ) : null}

        {/* Findings — accent-styled, individually dismissable */}
        {hasPending ? (
          <div className="space-y-1.5">
            <div
              className="font-mono text-[10px] uppercase tracking-label mt-2"
              style={{ color: "var(--color-accent)" }}
            >
              Needs attention
            </div>
            {pending.map((pa) => (
              <div
                key={pa.id}
                className="flex items-start gap-2 font-sans text-[12.5px]"
                style={{ color: "var(--color-text)" }}
              >
                <span
                  className="mt-1.5 shrink-0 h-[4px] w-[4px] rounded-full dot-glow-accent"
                  style={{ backgroundColor: "var(--color-accent)" }}
                />
                <span className="flex-1">{pa.message}</span>
                <button
                  type="button"
                  onClick={() => handleDismissItem(pa.id)}
                  title="Dismiss — won't resurface for a week"
                  className="font-mono text-[10px] shrink-0 px-1"
                  style={{ color: "var(--color-text-dim)" }}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        ) : null}

        <div className="mt-3 flex items-center justify-between">
          <span
            className="font-sans text-[12.5px]"
            style={{ color: "var(--color-text)" }}
          >
            {hasPending
              ? "Tell me what to do about these, or just start chatting."
              : "What are you working on?"}
          </span>
          <button
            type="button"
            onClick={() => void handleDismissAll()}
            className="font-mono text-[9px] uppercase tracking-label"
            style={{ color: "var(--color-text-dim)" }}
          >
            dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add ui/src/api.ts ui/src/components/CatchupBanner.tsx
git commit -m "feat(ui): per-finding dismiss in catch-up banner"
```

---

### Task 11: Final verification

- [ ] **Step 1: Full test suite + lint**

```bash
python -m pytest tests/ -v
ruff check src tests
ruff format --check src tests
```

Expected: all tests pass, no lint findings.

- [ ] **Step 2: Spec cross-check**

Walk `docs/superpowers/specs/2026-06-11-learned-norms-engine-design.md` section by section and confirm each requirement maps to shipped code: EntityProfile fields → `profiles.py`; gates (14d/8 cycles, 8 away samples) → `profiles.py` constants; detectors + margins → `detectors.py`; 3.5σ + persistence → `anomaly.py`; keyed/cap/TTL/auto-resolve/cooldown/migration → `findings.py` + `scheduler.py`; surfacing → routes + `CatchupBanner.tsx`.

- [ ] **Step 3: Commit any stragglers and report**

Summarize for the user what changed and remind them: after the HA rebuild, the banner will be empty at first (migration wipes the backlog) and duration/while-away findings stay quiet until profiles mature (~2 weeks of transitions for duration; away-time sampling for while-away). The unavailable/stale/anomaly checks keep working immediately, now bounded and self-cleaning.
