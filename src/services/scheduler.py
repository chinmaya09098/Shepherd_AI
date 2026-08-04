"""
Automated background scheduler — zero-human-intervention jobs.

Jobs registered:
  - sync_from_brokerware       : daily 02:00 UTC — seed customer_retry_config from Brokerware
  - expire_stale_conversations : daily 03:00 UTC — mark pickup-date-passed threads as expired
  - auto_register_webhooks     : on startup + every 47 h — keep Graph subscriptions alive
  - run_health_checks          : every 15 min — check auth / DB / Graph; alert on failure

Usage::
    from src.services.scheduler import start_scheduler, stop_scheduler

    start_scheduler()   # non-blocking; runs all jobs in background threads
    ...
    stop_scheduler()    # graceful shutdown (waits for running jobs to finish)

Standalone::
    python run_scheduler.py
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.utils.logger import get_logger

logger = get_logger(__name__)

# Module-level scheduler instance — one per process.
_scheduler: BackgroundScheduler | None = None


# ---------------------------------------------------------------------------
# Job wrappers — thin shells that import lazily to keep startup fast.
# ---------------------------------------------------------------------------

def _job_sync() -> None:
    """Daily: seed customer_retry_config from all Brokerware tenants."""
    logger.info("[scheduler] Running job: sync_from_brokerware")
    try:
        from src.db.retry_config_repository import sync_from_brokerware
        count = sync_from_brokerware()
        logger.info("[scheduler] sync_from_brokerware: %d row(s) upserted", count)
    except Exception as exc:
        logger.error("[scheduler] sync_from_brokerware failed: %s", exc, exc_info=True)


def _job_expire() -> None:
    """Daily: expire conversations whose pickup date has already passed."""
    logger.info("[scheduler] Running job: expire_stale_conversations")
    try:
        from src.services.followup_orchestrator import expire_stale_conversations
        count = expire_stale_conversations()
        logger.info("[scheduler] expire_stale_conversations: %d expired", count)
    except Exception as exc:
        logger.error("[scheduler] expire_stale_conversations failed: %s", exc, exc_info=True)


def _job_webhooks() -> None:
    """Every 47 h: ensure all inbox subscriptions are registered/renewed.

    Skipped gracefully when GRAPH_WEBHOOK_NOTIFICATION_URL is not configured
    (e.g. running locally before the Function App is deployed).
    """
    from src.config import Config
    if not Config.GRAPH_WEBHOOK_NOTIFICATION_URL:
        logger.info(
            "[scheduler] auto_register_all_mailboxes: GRAPH_WEBHOOK_NOTIFICATION_URL "
            "not configured — skipping webhook registration"
        )
        return

    logger.info("[scheduler] Running job: auto_register_all_mailboxes")
    try:
        from src.services.webhook_subscription_manager import WebhookSubscriptionManager
        mgr    = WebhookSubscriptionManager()
        result = mgr.auto_register_all_mailboxes()
        ok     = sum(1 for v in result.values() if v)
        logger.info(
            "[scheduler] auto_register_all_mailboxes: %d/%d mailbox(es) covered",
            ok, len(result),
        )
    except Exception as exc:
        logger.error("[scheduler] auto_register_all_mailboxes failed: %s", exc, exc_info=True)


def _job_health() -> None:
    """Every 15 min: run health checks and alert on failures."""
    logger.info("[scheduler] Running job: run_checks")
    try:
        from src.services.health_monitor import run_checks
        results  = run_checks()
        failures = [r for r in results if not r.ok]
        if failures:
            logger.warning(
                "[scheduler] health check FAILED: %s",
                ", ".join(r.name for r in failures),
            )
        else:
            logger.info("[scheduler] health check OK (%d checks passed)", len(results))
    except Exception as exc:
        logger.error("[scheduler] run_checks failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_scheduler() -> BackgroundScheduler:
    """Start the background scheduler with all registered jobs.

    Idempotent — calling start_scheduler() when already running returns the
    existing scheduler without registering duplicate jobs.

    Returns the running BackgroundScheduler instance.
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.info("[scheduler] Already running — skipping start")
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")

    # ── Job 1: daily Brokerware sync (02:00 UTC) ──────────────────────────
    _scheduler.add_job(
        _job_sync,
        trigger=CronTrigger(hour=2, minute=0, timezone="UTC"),
        id="sync_from_brokerware",
        name="Daily Brokerware customer sync",
        replace_existing=True,
        misfire_grace_time=3600,  # run up to 1 h late if the process was down
    )

    # ── Job 2: daily stale-conversation expiry (03:00 UTC) ────────────────
    _scheduler.add_job(
        _job_expire,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="expire_stale_conversations",
        name="Daily stale conversation expiry",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── Job 3: webhook subscription renewal (every 47 h, also on startup) ─
    _scheduler.add_job(
        _job_webhooks,
        trigger=IntervalTrigger(hours=47),
        id="auto_register_webhooks",
        name="Graph webhook subscription renewal",
        replace_existing=True,
        next_run_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        # next_run_time=now means it fires immediately on startup as well.
    )

    # ── Job 4: health checks (every 15 min) ───────────────────────────────
    _scheduler.add_job(
        _job_health,
        trigger=IntervalTrigger(minutes=15),
        id="run_health_checks",
        name="System health checks",
        replace_existing=True,
    )

    _scheduler.start()

    logger.info(
        "[scheduler] Started with %d job(s): %s",
        len(_scheduler.get_jobs()),
        ", ".join(j.id for j in _scheduler.get_jobs()),
    )
    return _scheduler


def stop_scheduler() -> None:
    """Gracefully stop the scheduler, waiting for any running jobs to finish."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=True)
        logger.info("[scheduler] Stopped")
    _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    """Return the current scheduler instance, or None if not started."""
    return _scheduler
