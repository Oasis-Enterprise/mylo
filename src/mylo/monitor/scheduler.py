"""APScheduler wiring for background jobs.

Two job slots:

* **nightly** — runs the memory reconciler + baseline recompute at
  02:00 local time (or the first available minute after HA boots if
  it was off at 2 AM). Frequency controlled by
  ``config.sync_frequency`` (nightly / weekly / manual).
* **hourly** — lightweight availability sweep (no LLM). Checks for
  entities that went ``unavailable``, automations that stopped
  firing, and device-tracker changes.

APScheduler 3.x's ``AsyncIOScheduler`` integrates with the running
event loop via aiohttp's startup/cleanup hooks. Jobs that need
server singletons (ws_client, registries, memory store) receive
the aiohttp ``Application`` instance so they can read ``AppKeys``
without circular imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from mylo.logging_setup import get_logger

if TYPE_CHECKING:
    from aiohttp import web

log = get_logger(__name__)


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

    # ── Hourly job (availability sweep) ─────────────────────────────
    if config.proactive_notifications:
        scheduler.add_job(
            _hourly_job,
            trigger=IntervalTrigger(hours=1),
            args=[app],
            id="mylo_hourly",
            name="Mylo hourly availability sweep",
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

    log.info("nightly.finished")


async def _hourly_job(app: web.Application) -> None:
    """Lightweight availability sweep + anomaly check."""
    from mylo.monitor.anomaly import check_anomalies
    from mylo.monitor.hourly import run_hourly_check
    from mylo.monitor.notifier import Notifier
    from mylo.server.app import AppKeys

    config = app[AppKeys.CONFIG]
    ws_client = app[AppKeys.HA_CLIENT]
    registries = app.get(AppKeys.REGISTRIES)
    store = app[AppKeys.MEMORY]
    notifier = Notifier(ws_client=ws_client, config=config, memory=store.current())

    log.info("hourly.started")

    # 1. Availability sweep.
    try:
        findings = await run_hourly_check(
            ws_client=ws_client,
            registries=registries,
        )
        for finding in findings:
            # Map finding IDs to notification types for suppression.
            ntype = "unavailable" if finding["id"] == "unavailable" else "stale_automation"
            await notifier.send(
                title=finding["title"],
                message=finding["message"],
                notification_id=f"mylo_hourly_{finding['id']}",
                severity=finding.get("severity", "normal"),
                notification_type=ntype,
            )
        if findings:
            log.info("hourly.findings", count=len(findings))
    except Exception:
        log.exception("hourly.check_failed")

    # 2. Anomaly check (only if baselines exist).
    try:
        memory = store.current()
        if memory.baselines.entities:
            anomalies = await check_anomalies(
                ws_client=ws_client,
                baselines=memory.baselines,
            )
            for anomaly in anomalies:
                await notifier.send(
                    title=anomaly["title"],
                    message=anomaly["message"],
                    notification_id=f"mylo_anomaly_{anomaly['id']}",
                    severity=anomaly.get("severity", "normal"),
                    notification_type="anomaly",
                    entity_id=anomaly.get("entity_id"),
                )
            if anomalies:
                log.info("hourly.anomalies", count=len(anomalies))
    except Exception:
        log.exception("hourly.anomaly_failed")

    log.info("hourly.finished")
