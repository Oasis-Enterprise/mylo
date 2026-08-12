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

"""APScheduler wiring for background jobs.

Two job slots:

* **nightly** — runs the memory reconciler + baseline recompute at
  02:00 local time (or the first available minute after HA boots if
  it was off at 2 AM). Frequency controlled by
  ``config.sync_frequency`` (nightly / weekly / manual). Also folds
  recent transitions into learned entity profiles.
* **hourly** — lightweight sweep (no LLM). Checks for entities that
  went ``unavailable``, automations that stopped firing, anomalies,
  and learned duration/while-away findings. All findings flow through
  the bounded findings store; notifications fire only on new findings.

APScheduler 3.x's ``AsyncIOScheduler`` integrates with the running
event loop via aiohttp's startup/cleanup hooks. Jobs that need
server singletons (ws_client, registries, memory store) receive
the aiohttp ``Application`` instance so they can read ``AppKeys``
without circular imports.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from mylo.logging_setup import get_logger

if TYPE_CHECKING:
    from aiohttp import web

log = get_logger(__name__)

# Serializes profiles.json load-modify-save between nightly fold and
# hourly learned checks — the hourly holds its ProfileSet across an
# await, so an unguarded nightly fold could be silently overwritten.
_profiles_lock = asyncio.Lock()


async def start_scheduler(app: web.Application) -> AsyncIOScheduler:
    """Create, configure, and start the background scheduler.

    Returns the scheduler so the caller can stash it on the app for
    cleanup. Jobs are registered but won't fire until ``start()`` is
    called — we call it at the end of this function, which means
    jobs can fire as soon as the event loop yields.
    """
    from mylo.config import AppConfig
    from mylo.server.app import AppKeys

    config: AppConfig = app[AppKeys.CONFIG]
    scheduler = AsyncIOScheduler(timezone="UTC")

    # ── Nightly job (reconciler + baselines) ────────────────────────
    if config.sync_frequency != "manual":
        nightly_trigger = _build_nightly_trigger(config)
        scheduler.add_job(
            _nightly_job,
            trigger=nightly_trigger,
            args=[app],
            id="mylo_nightly",
            name="Mylo nightly sync + baselines",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        log.info(
            "scheduler.nightly_registered",
            frequency=config.sync_frequency,
            trigger=str(nightly_trigger),
        )

    # ── Hourly job (availability + anomaly + learned checks) ────────
    if config.proactive_notifications:
        scheduler.add_job(
            _hourly_job,
            trigger=IntervalTrigger(hours=1),
            args=[app],
            id="mylo_hourly",
            name="Mylo hourly monitor sweep",
            replace_existing=True,
            misfire_grace_time=1800,
        )
        log.info("scheduler.hourly_registered")

    scheduler.start()
    log.info("scheduler.started", jobs=len(scheduler.get_jobs()))
    return scheduler


async def stop_scheduler(scheduler: AsyncIOScheduler | None) -> None:
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        log.info("scheduler.stopped")


def _build_nightly_trigger(config: Any) -> CronTrigger:
    """Build a cron trigger based on the sync_frequency config."""
    if config.sync_frequency == "weekly":
        return CronTrigger(day_of_week="sun", hour=2, minute=0)
    # Default: nightly at 02:00 UTC.
    return CronTrigger(hour=2, minute=0)


# ─── Job implementations ────────────────────────────────────────────────────


async def _nightly_job(app: web.Application) -> None:
    """Reconcile memory + recompute baselines.

    Failures are logged but never crash the scheduler — a bad night
    shouldn't prevent the next run.
    """
    from mylo.memory.reconciler import run_sync
    from mylo.monitor.baselines import recompute_baselines
    from mylo.monitor.notifier import Notifier
    from mylo.server.app import AppKeys

    config = app[AppKeys.CONFIG]
    store = app[AppKeys.MEMORY]
    provider = app.get(AppKeys.PROVIDER)
    registries = app.get(AppKeys.REGISTRIES)
    notifier = Notifier(
        ws_client=app[AppKeys.HA_CLIENT],
        config=config,
        memory=store.current(),
    )

    log.info("nightly.started")

    # 1. Memory reconciliation.
    try:
        from typing import cast

        from mylo.memory.pruner import apply_prune
        from mylo.memory.reconciler import ReconcileProvider
        from mylo.memory.scratchpad import drain_scratchpad

        result = await run_sync(
            store=store,
            provider=cast("ReconcileProvider | None", provider),
            registries=registries,
            model=config.reconciliation_model,
            mylo_data_dir=config.mylo_data_dir,
        )

        if result.updated is not None:
            memory_to_save = result.updated
            if result.prune_report.total > 0:
                memory_to_save = apply_prune(memory_to_save, result.prune_report)
            await store.save(memory_to_save, note=f"nightly sync: {result.summary}")
            drain_scratchpad(config.mylo_data_dir)
        elif result.prune_report.total > 0:
            # Sync produced no merge (e.g. memory too large to reconcile),
            # but we can still safely shrink it by applying the prune plan.
            # Critical for recovering a runaway patterns list that's
            # blocking the LLM merge — next run then fits.
            pruned = apply_prune(store.current(), result.prune_report)
            await store.save(
                pruned, note=f"nightly prune-only: dropped {result.prune_report.total} items"
            )
            log.info("nightly.prune_only", dropped=result.prune_report.total)

        if result.conflicts_added > 0:
            await notifier.send(
                title="Memory sync: conflicts detected",
                message=(
                    f"{result.conflicts_added} conflict(s) found. Open the Memory tab to review."
                ),
                notification_id="mylo_nightly_conflicts",
                severity="normal",
                notification_type="sync_conflict",
            )
        log.info("nightly.sync_done", summary=result.summary)
    except Exception:
        log.exception("nightly.sync_failed")

    # 2. Baseline recompute.
    try:
        ws_client = app[AppKeys.HA_CLIENT]
        updated_baselines = await recompute_baselines(
            ws_client=ws_client,
            memory=store.current(),
        )
        if updated_baselines:
            mem = store.current()
            mem.baselines = updated_baselines
            await store.save(mem, note="nightly: baselines recomputed")
            log.info("nightly.baselines_done")
    except Exception:
        log.exception("nightly.baselines_failed")

    # 3. Behavioral pattern detection.
    try:
        from mylo.memory.pruner import enforce_pattern_cap
        from mylo.monitor.behavioral import detect_patterns
        from mylo.monitor.transitions import TransitionLogger

        raw_logger = app.get(AppKeys.TRANSITIONS)
        transition_logger = raw_logger if isinstance(raw_logger, TransitionLogger) else None
        if transition_logger is not None:
            mem = store.current()
            new_patterns, refreshed = detect_patterns(transition_logger, mem)
            if new_patterns:
                mem.patterns.extend(new_patterns)
            # Cap AFTER detection — capping only in the pre-detection
            # prune let the detector re-add ~1000 just-pruned patterns
            # from the unchanged transition window every night.
            dropped = enforce_pattern_cap(mem)
            if new_patterns or refreshed or dropped:
                await store.save(
                    mem,
                    note=(
                        f"nightly: {len(new_patterns)} behavioral patterns detected, "
                        f"{refreshed} refreshed, {dropped} over cap"
                    ),
                )
                log.info(
                    "nightly.behavioral_patterns",
                    new=len(new_patterns),
                    refreshed=refreshed,
                    dropped=dropped,
                )
    except Exception:
        log.exception("nightly.behavioral_failed")

    # 4. Fold transitions into learned profiles.
    try:
        from mylo.monitor.profiles import ProfileStore, fold_transitions
        from mylo.monitor.transitions import RETENTION_DAYS, TransitionLogger

        raw_logger = app.get(AppKeys.TRANSITIONS)
        transition_logger = raw_logger if isinstance(raw_logger, TransitionLogger) else None
        if transition_logger is not None:
            profile_store = ProfileStore(config.mylo_data_dir)
            async with _profiles_lock:
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

    log.info("nightly.finished")


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
            is_new = False
            if not findings_store.in_cooldown(memory, ntype, entity_id, now):
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
            # The aggregated unavailable finding shares one key, so a
            # refresh (is_new=False) can still describe a brand-new
            # outage batch — run_hourly_check already throttles those
            # to newly-unavailable entities, so notify them even while
            # the banner item is dismissed (dismiss silences the banner,
            # not outage alerts; type-level suppression filters are the
            # lever for muting those).
            if is_new or ntype == "unavailable":
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
        async with _profiles_lock:
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
