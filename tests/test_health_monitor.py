"""
Tests for src/services/health_monitor.py

Covers:
  - check_brokerware_auth : passes / fails based on _fetch_token
  - check_db_connection   : passes / DB unconfigured / DB error
  - check_graph_auth      : passes / empty token / exception
  - send_alert            : webhook POST called / email sent / no-op when no channels
  - run_checks            : all pass, partial failure triggers send_alert
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub src.secrets before src.config is imported
# ---------------------------------------------------------------------------
sys.modules.setdefault(
    "src.secrets",
    types.SimpleNamespace(load_secrets_from_keyvault=lambda: None),
)

# Stub heavy Azure SDK modules so the import chain doesn't fail.
for _mod in (
    "azure.storage.blob",
    "azure.storage.queue",
    "azure.search.documents",
    "azure.monitor.opentelemetry",
    "applicationinsights",
):
    sys.modules.setdefault(_mod, MagicMock())

from src.config import Config                         # noqa: E402
from src.services.health_monitor import (             # noqa: E402
    check_brokerware_auth,
    check_db_connection,
    check_graph_auth,
    send_alert,
    run_checks,
    HealthCheckResult,
    _send_webhook_alert,
    _send_email_alert,
)


# ===========================================================================
# check_brokerware_auth
# ===========================================================================

class TestCheckBrokerwareAuth(unittest.TestCase):

    def test_passes_when_token_fetched(self):
        with patch("src.services.brokerware_client._fetch_token"), \
             patch.object(Config, "BROKERWARE_MAILBOX_TENANT_MAP", "{}"):
            result = check_brokerware_auth()
        self.assertTrue(result.ok)
        self.assertEqual(result.name, "brokerware_auth")

    def test_fails_when_token_fetch_raises(self):
        with patch("src.services.brokerware_client._fetch_token",
                   side_effect=Exception("Connection refused")), \
             patch.object(Config, "BROKERWARE_CLIENT_ID", "cid"), \
             patch.object(Config, "BROKERWARE_CLIENT_SECRET", "secret"), \
             patch.object(Config, "BROKERWARE_MAILBOX_TENANT_MAP", "{}"):
            result = check_brokerware_auth()
        self.assertFalse(result.ok)
        self.assertIn("Connection refused", result.detail)

    def test_skips_unconfigured_tenant(self):
        """A tenant with no client_id is silently skipped (no failure)."""
        with patch.object(Config, "BROKERWARE_CLIENT_ID", ""), \
             patch.object(Config, "BROKERWARE_CLIENT_SECRET", ""), \
             patch.object(Config, "BROKERWARE_MAILBOX_TENANT_MAP", "{}"):
            result = check_brokerware_auth()
        self.assertTrue(result.ok)


# ===========================================================================
# check_db_connection
# ===========================================================================

class TestCheckDbConnection(unittest.TestCase):

    def test_passes_when_db_available(self):
        mock_cm = MagicMock()
        mock_cm.__enter__ = lambda s: MagicMock()
        mock_cm.__exit__ = MagicMock(return_value=False)
        with patch("src.db.database.init_db", return_value=True), \
             patch("src.db.database.get_session", return_value=mock_cm):
            result = check_db_connection()
        self.assertTrue(result.ok)

    def test_fails_when_init_db_returns_false(self):
        with patch("src.db.database.init_db", return_value=False):
            result = check_db_connection()
        self.assertFalse(result.ok)
        self.assertIn("init_db()", result.detail)

    def test_fails_when_session_raises(self):
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(side_effect=Exception("timeout"))
        mock_cm.__exit__ = MagicMock(return_value=False)
        with patch("src.db.database.init_db", return_value=True), \
             patch("src.db.database.get_session", return_value=mock_cm):
            result = check_db_connection()
        self.assertFalse(result.ok)
        self.assertIn("timeout", result.detail)


# ===========================================================================
# check_graph_auth
# ===========================================================================

class TestCheckGraphAuth(unittest.TestCase):

    def test_passes_with_valid_token(self):
        with patch("src.services.graph_client.get_app_token", return_value="tok123"):
            result = check_graph_auth()
        self.assertTrue(result.ok)

    def test_fails_when_token_empty(self):
        with patch("src.services.graph_client.get_app_token", return_value=""):
            result = check_graph_auth()
        self.assertFalse(result.ok)
        self.assertIn("empty token", result.detail)

    def test_fails_when_exception_raised(self):
        with patch("src.services.graph_client.get_app_token",
                   side_effect=Exception("AADSTS70011")):
            result = check_graph_auth()
        self.assertFalse(result.ok)
        self.assertIn("AADSTS70011", result.detail)


# ===========================================================================
# _send_webhook_alert
# ===========================================================================

class TestSendWebhookAlert(unittest.TestCase):

    def test_posts_to_webhook_url(self):
        failures = [HealthCheckResult(name="db_connection", ok=False, detail="timeout")]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp) as mock_post, \
             patch.object(Config, "ALERT_WEBHOOK_URL", "https://hooks.example.com/alert"):
            _send_webhook_alert("alert body", failures)
        mock_post.assert_called_once()
        url_arg = mock_post.call_args[0][0]
        self.assertEqual(url_arg, "https://hooks.example.com/alert")

    def test_no_op_when_url_not_configured(self):
        with patch("requests.post") as mock_post, \
             patch.object(Config, "ALERT_WEBHOOK_URL", ""):
            _send_webhook_alert("body", [])
        mock_post.assert_not_called()

    def test_handles_post_exception_gracefully(self):
        failures = [HealthCheckResult(name="graph_auth", ok=False, detail="err")]
        with patch("requests.post", side_effect=Exception("network error")), \
             patch.object(Config, "ALERT_WEBHOOK_URL", "https://hooks.example.com/alert"):
            # Should not raise
            _send_webhook_alert("body", failures)


# ===========================================================================
# _send_email_alert
# ===========================================================================

class TestSendEmailAlert(unittest.TestCase):

    def test_sends_email_via_graph(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("src.services.graph_client.get_app_token", return_value="tok"), \
             patch("httpx.post", return_value=mock_resp) as mock_post, \
             patch.object(Config, "ALERT_FROM_EMAIL", "alerts@company.com"), \
             patch.object(Config, "ALERT_EMAIL_TO",   "admin@company.com"):
            _send_email_alert("alert body")
        mock_post.assert_called_once()

    def test_no_op_when_email_not_configured(self):
        with patch("httpx.post") as mock_post, \
             patch.object(Config, "ALERT_FROM_EMAIL", ""), \
             patch.object(Config, "ALERT_EMAIL_TO",   ""):
            _send_email_alert("body")
        mock_post.assert_not_called()


# ===========================================================================
# send_alert
# ===========================================================================

class TestSendAlert(unittest.TestCase):

    def test_no_op_when_no_failures(self):
        with patch("src.services.health_monitor._send_webhook_alert") as wh, \
             patch("src.services.health_monitor._send_email_alert") as em:
            send_alert([])
        wh.assert_not_called()
        em.assert_not_called()

    def test_calls_both_channels_on_failure(self):
        failures = [HealthCheckResult(name="db_connection", ok=False, detail="err")]
        with patch("src.services.health_monitor._send_webhook_alert") as wh, \
             patch("src.services.health_monitor._send_email_alert") as em:
            send_alert(failures)
        wh.assert_called_once()
        em.assert_called_once()


# ===========================================================================
# run_checks
# ===========================================================================

class TestRunChecks(unittest.TestCase):

    def test_all_pass_no_alert(self):
        ok = HealthCheckResult(name="x", ok=True)
        with patch("src.services.health_monitor.check_db_connection",   return_value=ok), \
             patch("src.services.health_monitor.check_brokerware_auth", return_value=ok), \
             patch("src.services.health_monitor.check_graph_auth",      return_value=ok), \
             patch("src.services.health_monitor.send_alert") as mock_alert:
            results = run_checks()
        mock_alert.assert_not_called()
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.ok for r in results))

    def test_partial_failure_triggers_alert(self):
        ok   = HealthCheckResult(name="x", ok=True)
        fail = HealthCheckResult(name="db_connection", ok=False, detail="err")
        with patch("src.services.health_monitor.check_db_connection",   return_value=fail), \
             patch("src.services.health_monitor.check_brokerware_auth", return_value=ok), \
             patch("src.services.health_monitor.check_graph_auth",      return_value=ok), \
             patch("src.services.health_monitor.send_alert") as mock_alert:
            results = run_checks()
        mock_alert.assert_called_once()
        failures_arg = mock_alert.call_args[0][0]
        self.assertEqual(len(failures_arg), 1)
        self.assertEqual(failures_arg[0].name, "db_connection")

    def test_unexpected_exception_in_check_handled(self):
        """An exception inside a check function is caught and stored as a failure."""
        ok = HealthCheckResult(name="x", ok=True)
        with patch("src.services.health_monitor.check_db_connection",
                   side_effect=RuntimeError("kaboom")), \
             patch("src.services.health_monitor.check_brokerware_auth", return_value=ok), \
             patch("src.services.health_monitor.check_graph_auth",      return_value=ok), \
             patch("src.services.health_monitor.send_alert"):
            results = run_checks()
        # Exactly one failure containing the exception message.
        failures = [r for r in results if not r.ok]
        self.assertEqual(len(failures), 1)
        self.assertIn("kaboom", failures[0].detail)


if __name__ == "__main__":
    unittest.main()
