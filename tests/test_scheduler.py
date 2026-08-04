"""
Tests for src/services/scheduler.py

Covers:
  - start_scheduler : registers all 4 jobs, returns running scheduler
  - stop_scheduler  : shuts down the scheduler
  - idempotency     : calling start_scheduler() twice does not duplicate jobs
  - job wrappers    : each _job_* function calls the correct underlying function
  - get_scheduler   : returns None before start, instance after start
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub src.secrets before any src.* import
# ---------------------------------------------------------------------------
sys.modules.setdefault(
    "src.secrets",
    types.SimpleNamespace(load_secrets_from_keyvault=lambda: None),
)

# Stub heavy third-party modules used transitively by scheduler imports.
for _mod in (
    "azure.storage.blob",
    "azure.storage.queue",
    "azure.search.documents",
    "azure.monitor.opentelemetry",
    "applicationinsights",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.background",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
):
    sys.modules.setdefault(_mod, MagicMock())

# Provide realistic-enough stub classes so the scheduler module can be imported.
_bg_scheduler_instance = MagicMock()
_bg_scheduler_instance.running = False
_bg_scheduler_instance.get_jobs.return_value = []

_BackgroundScheduler = MagicMock(return_value=_bg_scheduler_instance)
_CronTrigger        = MagicMock()
_IntervalTrigger    = MagicMock()

sys.modules["apscheduler.schedulers.background"].BackgroundScheduler = _BackgroundScheduler
sys.modules["apscheduler.triggers.cron"].CronTrigger                 = _CronTrigger
sys.modules["apscheduler.triggers.interval"].IntervalTrigger         = _IntervalTrigger

import src.services.scheduler as _sched_mod           # noqa: E402
from src.services.scheduler import (                   # noqa: E402
    start_scheduler,
    stop_scheduler,
    get_scheduler,
    _job_sync,
    _job_expire,
    _job_webhooks,
    _job_health,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _reset_scheduler():
    """Reset module-level scheduler state between tests."""
    _sched_mod._scheduler = None
    _bg_scheduler_instance.running = False
    _bg_scheduler_instance.reset_mock()
    _bg_scheduler_instance.get_jobs.return_value = []


# ===========================================================================
# start_scheduler / stop_scheduler / get_scheduler
# ===========================================================================

class TestSchedulerLifecycle(unittest.TestCase):

    def setUp(self):
        _reset_scheduler()

    def test_start_returns_scheduler_instance(self):
        _bg_scheduler_instance.running = True
        result = start_scheduler()
        self.assertIsNotNone(result)

    def test_start_calls_scheduler_start(self):
        _bg_scheduler_instance.running = True
        start_scheduler()
        _bg_scheduler_instance.start.assert_called_once()

    def test_start_registers_four_jobs(self):
        start_scheduler()
        self.assertEqual(_bg_scheduler_instance.add_job.call_count, 4)

    def test_idempotent_when_already_running(self):
        _bg_scheduler_instance.running = True
        _sched_mod._scheduler = _bg_scheduler_instance
        start_scheduler()
        # Should not call start() or add_job() again
        _bg_scheduler_instance.start.assert_not_called()
        _bg_scheduler_instance.add_job.assert_not_called()

    def test_stop_calls_shutdown(self):
        _bg_scheduler_instance.running = True
        _sched_mod._scheduler = _bg_scheduler_instance
        stop_scheduler()
        _bg_scheduler_instance.shutdown.assert_called_once_with(wait=True)

    def test_stop_sets_scheduler_to_none(self):
        _bg_scheduler_instance.running = True
        _sched_mod._scheduler = _bg_scheduler_instance
        stop_scheduler()
        self.assertIsNone(_sched_mod._scheduler)

    def test_stop_noop_when_not_running(self):
        _sched_mod._scheduler = None
        stop_scheduler()  # should not raise

    def test_get_scheduler_returns_none_initially(self):
        self.assertIsNone(get_scheduler())

    def test_get_scheduler_returns_instance_after_start(self):
        _bg_scheduler_instance.running = True
        start_scheduler()
        self.assertIsNotNone(get_scheduler())

    def test_job_ids_registered(self):
        """Verify all 4 expected job IDs are passed to add_job."""
        start_scheduler()
        job_ids = {call[1]["id"] for call in _bg_scheduler_instance.add_job.call_args_list}
        self.assertIn("sync_from_brokerware",       job_ids)
        self.assertIn("expire_stale_conversations", job_ids)
        self.assertIn("auto_register_webhooks",     job_ids)
        self.assertIn("run_health_checks",          job_ids)


# ===========================================================================
# Job wrapper functions
# ===========================================================================

class TestJobWrappers(unittest.TestCase):

    def test_job_sync_calls_sync_from_brokerware(self):
        with patch("src.db.retry_config_repository.sync_from_brokerware",
                   return_value=5) as mock_sync:
            _job_sync()
        mock_sync.assert_called_once()

    def test_job_sync_handles_exception(self):
        with patch("src.db.retry_config_repository.sync_from_brokerware",
                   side_effect=Exception("DB down")):
            _job_sync()  # must not raise

    def test_job_expire_calls_expire_stale_conversations(self):
        with patch("src.services.followup_orchestrator.expire_stale_conversations",
                   return_value=2) as mock_expire:
            _job_expire()
        mock_expire.assert_called_once()

    def test_job_expire_handles_exception(self):
        with patch("src.services.followup_orchestrator.expire_stale_conversations",
                   side_effect=RuntimeError("blob error")):
            _job_expire()  # must not raise

    def test_job_webhooks_calls_auto_register(self):
        mock_mgr = MagicMock()
        mock_mgr.auto_register_all_mailboxes.return_value = {"user@x.com": "sub-id"}
        with patch("src.services.webhook_subscription_manager.WebhookSubscriptionManager",
                   return_value=mock_mgr):
            _job_webhooks()
        mock_mgr.auto_register_all_mailboxes.assert_called_once()

    def test_job_webhooks_handles_exception(self):
        with patch("src.services.webhook_subscription_manager.WebhookSubscriptionManager",
                   side_effect=Exception("Graph down")):
            _job_webhooks()  # must not raise

    def test_job_health_calls_run_checks(self):
        ok = MagicMock(ok=True)
        with patch("src.services.health_monitor.run_checks", return_value=[ok, ok]) as mock_hc:
            _job_health()
        mock_hc.assert_called_once()

    def test_job_health_handles_exception(self):
        with patch("src.services.health_monitor.run_checks",
                   side_effect=Exception("monitor error")):
            _job_health()  # must not raise


if __name__ == "__main__":
    unittest.main()
