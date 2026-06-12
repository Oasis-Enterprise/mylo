# Learned-Norms Engine — Design Spec

**Date:** 2026-06-11
**Status:** Approved
**Replaces:** fixed-threshold proactive suggestions (M9/M11 monitor noise rework)

## Problem

The monitoring feature (scheduler + hourly sweep + suggestions + z-score anomalies) is noisy
and data-hungry:

1. `pending_actions` in `context.yaml` grows forever — no cap, no TTL, no pruning of resolved
   items. Hourly sweeps append; nothing ever deletes.
2. Most alerts come from fixed thresholds (`device on > 4h`, `lock unlocked > 30min`, `any
   light on while away`) that have no concept of what is normal *for that entity*.
3. Z-score anomalies fire at 2.5σ against a 7-day baseline. Across ~2200 entities checked
   hourly, a ~1% false-positive rate produces a constant drip of findings.
4. The learning that does exist (`behavioral.py` time-of-day patterns from `transitions.jsonl`)
   is never consulted by the alerting path. Learning and alerting are disconnected systems.

## Goal

Confidence over frequency. Mylo learns each entity's normal behavior over time and only
surfaces deviations from *that entity's own history* — "this light has been on 8h and the
longest you've ever left it is 6h" — not rule-of-thumb thresholds. Quiet by default; an
entity earns the right to alert by accumulating observations.

## Decisions made

- **Cold start:** per-entity quiet-until-confident. No global learning window. No
  alert-immediately-with-confidence-label.
- **Fixed rules:** deleted, replaced by learned norms (not kept as opt-in toggles).
- **Surfacing:** small capped list in the catch-up banner with auto-expiry; not a digest,
  not chat-only.
- **Approach:** learned-norms engine (compact per-entity aggregates), not incremental
  hardening of the existing rules and not full statistical models (7×24 occupancy matrices,
  seasonality). The profile design below can grow into richer models later if needed.

## Architecture

```
Transition stream (existing 14-day JSONL, transitions.py)
        │ nightly fold-in
        ▼
EntityProfile  (compact rolling aggregates, profiles.json)
  • on-duration stats: cycle count, exact max, log-bucket histogram → p95
  • active-hours histogram (24 buckets)
  • away behavior: on-while-away count vs observed away periods
  • coverage: first_seen, days_observed, total_events → confidence
        │ gate: eligible only when confident
        ▼
Detectors (hourly sweep)
  • duration anomaly (replaces device_running_long / unlocked_too_long)
  • while-away anomaly (replaces on_while_away)
  • numeric z-score (existing baselines, hardened: 3.5σ + 2-check persistence)
  • unavailable entities + stale automations (kept as-is)
        │
        ▼
Findings store (keyed, capped at 5, auto-resolve, 48h TTL)
        │
        ▼
Catch-up banner (≤5 items, plain-English "why", per-item dismiss = 7-day cooldown)
```

## Components

### 1. EntityProfile (`src/mylo/monitor/profiles.py`, new)

One profile per entity in the domains `transitions.py` already watches (light, switch, lock,
cover, person, binary_sensor, climate, media_player, fan).

Fields:

- `entity_id`, `first_seen`, `last_updated`
- `days_observed` — distinct calendar days with at least one event
- `total_events`
- `cycle_count` — completed on→off (or unlocked→locked, open→closed) cycles
- `max_duration_s` — exact longest observed cycle
- `duration_histogram` — counts in log-scale buckets: ≤5m, ≤15m, ≤30m, ≤1h, ≤2h, ≤4h,
  ≤8h, ≤16h, ≤24h, >24h. p95 derived from the histogram (upper bound of the bucket
  containing the 95th percentile).
- `active_hours` — 24 ints, event count per hour-of-day (kept for future use and
  explainability; not a detector input in v1)
- `away_samples`, `on_while_away_samples` — away behavior is learned by sampling, not
  reconstruction: a sweep that runs while everyone is away records one sample per
  light/switch entity (`away_samples += 1`, plus `on_while_away_samples += 1` if the
  entity is on). Reconstructing contiguous "away periods" from person transitions would
  require knowing initial states; sampling gives the same signal with far less code.
  Two guards keep the learning honest:
  - **At most one sample per calendar day** (`ProfileSet.last_away_sample_date`): hourly
    samples within one absence are autocorrelated — without this, a single 8-hour trip
    would fully qualify every entity and define its away norm. With it, the ≥8-sample
    gate means roughly 8 distinct away days.
  - **Only definitive person states count**: `unknown`/`unavailable` person entities are
    ignored when deciding "everyone away" — a tracker outage at 3am must not record
    poisoned samples or fire while-away alerts while the user is in bed. No definitive
    state ⇒ treated as not away.
  - Known residual: a real anomaly that persists across a sampled day teaches itself
    into the norm by one sample. Bounded to one sample per incident by the
    one-per-day rule; accepted.
  - While-away findings use their own confidence basis (`away_samples / 16`, capped
    at 1.0) so away-only entities aren't permanently the first evicted from the
    capped findings store.

Maintenance:

- A nightly job replays `transitions.jsonl` since the profile's `last_updated` watermark and
  folds completed cycles into the aggregates. Profiles accumulate knowledge beyond the 14-day
  raw retention; raw transitions stay small.
- Storage: `{mylo_data_dir}/profiles.json` — machine state, deliberately NOT in
  `context.yaml` (keeps the LLM-visible memory file small). ~200 bytes/entity.
- Corruption/missing file: rebuild from available transitions; affected entities simply
  re-enter the quiet period. Never fatal.

Confidence gate:

- Duration alerts: eligible when `days_observed ≥ 14` AND `cycle_count ≥ 8`.
- While-away alerts: eligible when `away_samples ≥ 8`.
- `confidence = min(1.0, days_observed / 21)` — used only to order findings, never shown
  as a number.

### 2. Detectors (hourly sweep, `scheduler.py` + new detector module)

All detectors consult the profile gate before emitting anything.

- **Duration anomaly** — for each entity currently in its "active" state (on / unlocked /
  open), compute current duration. Finding when
  `duration > max(max_duration_s × margin_max, p95 × margin_p95)`:
  - default domains: `margin_max = 1.25`, `margin_p95 = 2.0`
  - lock/door domains (lock, and binary_sensor device_class door/garage_door):
    `margin_max = 1.1`, `margin_p95 = 1.5`
  - Message includes the learned norm: "Kitchen light has been on 8h — the longest you've
    left it before is 6h."
- **While-away anomaly** — entity on while all `person.*` are `not_home`, AND
  `on_while_away_samples / away_samples < 0.20`. Entities routinely left on while
  away never fire.
- **Numeric z-score** (`anomaly.py`, hardened) — threshold 2.5σ → 3.5σ; a finding is
  emitted only after the same entity is anomalous on 2 consecutive hourly checks
  (persistence tracked in module state, like `_previously_unavailable`).
- **Kept unchanged:** unavailable-entity sweep and stale-automation check in `hourly.py`
  (already deduped); they now write into the findings store instead of `pending_actions`.

Deleted: `suggestions.py` detectors `on_while_away`, `unlocked_too_long`,
`device_running_long` and the `hourly.py` lights-on-while-away check.

Kept and rewired: suggestion outcome tracking (`Suggestion` counters), rejection-based
silencing (`MAX_REJECTIONS_BEFORE_SILENCE`), 3-acceptances → automation proposal, and
per-entity `NotificationSuppression`. Actionable findings ("Want me to turn it off?") flow
through the same accept/reject path as before.

### 3. Findings store (replaces `pending_actions` semantics)

- **Keyed:** one finding per `(type, entity_id)`. Re-detection updates `last_seen`; never
  appends a duplicate.
- **Auto-resolve:** each sweep re-checks active findings; if the condition has cleared
  (entity off / back in range / back online), the finding is deleted. No user action needed.
- **TTL:** findings older than 48h are deleted regardless.
- **Cap:** max 5 active findings; when full, the lowest-confidence finding is evicted.
- **Dismiss:** per-item dismiss sets a 7-day cooldown for that `(type, entity_id)` — the
  detector skips it during cooldown. Dismiss-all applies the cooldown to all current items.
- **Migration:** on first startup of the new version, the legacy `pending_actions` list in
  `context.yaml` is cleared (one-time, logged).
- Notification routing (`notifier.py`, quiet hours, `max_daily_notifications`) is unchanged —
  fewer findings simply means fewer notifications.

### 4. Surfacing (`CatchupBanner.tsx`, `routes_chat.py`)

Banner structure unchanged. Changes:

- Shows at most 5 findings, ordered by confidence.
- Each item shows the plain-English "why" (the learned norm is embedded in the message).
- Per-item dismiss button (new) in addition to dismiss-all; both call the cooldown API.
- No confidence numbers in the UI.

## Error handling

- Profile file unreadable → log warning, rebuild from transitions, entities re-enter quiet
  period.
- HA websocket/stat calls failing → that sweep is skipped; findings auto-resolve logic does
  not delete findings on a failed sweep (only on a successful re-check).
- Clock skew / DST: durations computed from UTC timestamps already stored in transitions.

## Testing

Unit tests with synthetic transition streams (no HA required):

1. Entity with consistent ~30-min cycles over 20 days → flags when on 4h.
2. Entity routinely on ~3h (p95 bucket ≤4h, max 3.5h) → silent at 4h, flags at 9h
   (threshold = max(3.5h × 1.25, 4h × 2) = 8h).
3. Entity with 10 days observed → silent regardless of duration (gate).
4. Lock with tight margins flags earlier than a light with the same history.
5. While-away: routine porch light (on in 80% of away periods) silent; rare one fires.
6. Z-score: single-check 4σ blip → no finding; two consecutive checks → finding.
7. Findings: dedup by key, auto-resolve on condition clear, 48h TTL, cap-5 eviction,
   dismiss cooldown honored.
8. Migration clears legacy `pending_actions` exactly once.

Behavior verification on the real HA instance via rebuild (user's standard workflow).

## Out of scope (explicitly)

- Weekday/weekend or seasonal modeling; 7×24 occupancy matrices.
- Using `active_hours` as a detector input (collected for explainability/future use only).
- Changes to `baselines.py` computation or the 7-day stats window.
- Changes to `behavioral.py` pattern detection or automation proposals beyond rewiring
  their inputs.
