"""
Tests for src/config.py

Covers:
  - Config.get_mailbox_user_ids(): GRAPH_MAILBOX_USER_IDS, fallback to USER_ID, empty
  - Config.get_max_followups(): DB row hit, DB miss (env-var fallback), global default
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub src.secrets so Config can be imported cleanly.
# ---------------------------------------------------------------------------
sys.modules.setdefault(
    "src.secrets",
    types.SimpleNamespace(load_secrets_from_keyvault=lambda: None),
)

from src.config import Config  # noqa: E402


# ===========================================================================
# get_mailbox_user_ids
# ===========================================================================

class TestGetMailboxUserIds(unittest.TestCase):

    def test_returns_list_from_user_ids_env_var(self):
        with patch.object(Config, "GRAPH_MAILBOX_USER_IDS",
                          "a@x.com,b@x.com,c@x.com"):
            ids = Config.get_mailbox_user_ids()
        self.assertEqual(ids, ["a@x.com", "b@x.com", "c@x.com"])

    def test_strips_whitespace_around_entries(self):
        with patch.object(Config, "GRAPH_MAILBOX_USER_IDS",
                          " a@x.com , b@x.com "):
            ids = Config.get_mailbox_user_ids()
        self.assertEqual(ids, ["a@x.com", "b@x.com"])

    def test_falls_back_to_single_user_id(self):
        with patch.object(Config, "GRAPH_MAILBOX_USER_IDS", ""), \
             patch.object(Config, "GRAPH_MAILBOX_USER_ID",   "single@x.com"):
            ids = Config.get_mailbox_user_ids()
        self.assertEqual(ids, ["single@x.com"])

    def test_returns_empty_list_when_neither_set(self):
        with patch.object(Config, "GRAPH_MAILBOX_USER_IDS", ""), \
             patch.object(Config, "GRAPH_MAILBOX_USER_ID",   None):
            ids = Config.get_mailbox_user_ids()
        self.assertEqual(ids, [])

    def test_skips_empty_tokens_in_csv(self):
        with patch.object(Config, "GRAPH_MAILBOX_USER_IDS", "a@x.com,,b@x.com,"):
            ids = Config.get_mailbox_user_ids()
        self.assertEqual(ids, ["a@x.com", "b@x.com"])

    def test_three_mailboxes_configured(self):
        """The live config has 3 mailboxes — verify they are all returned."""
        upns = (
            "Shepherd@3plsystems0.onmicrosoft.com,"
            "Shepherd1@3plsystems0.onmicrosoft.com,"
            "Shepherd2@3plsystems0.onmicrosoft.com"
        )
        with patch.object(Config, "GRAPH_MAILBOX_USER_IDS", upns):
            ids = Config.get_mailbox_user_ids()
        self.assertEqual(len(ids), 3)
        self.assertIn("Shepherd@3plsystems0.onmicrosoft.com", ids)
        self.assertIn("Shepherd2@3plsystems0.onmicrosoft.com", ids)


# ===========================================================================
# get_max_followups
# ===========================================================================

class TestGetMaxFollowups(unittest.TestCase):
    """
    get_max_followups lookup chain:
      1. DB row in customer_retry_config   (primary — only if row exists)
      2. FOLLOWUP_MAX_BY_CUSTOMER JSON env var  (legacy override)
      3. FOLLOWUP_DEFAULT_MAX              (global default, default 3)
    """

    def _make_session_cm(self, db_row):
        """Return a mock context-manager that yields a session.get() → db_row."""
        session = MagicMock()
        session.get.return_value = db_row
        return MagicMock(
            __enter__=lambda s: session,
            __exit__=MagicMock(return_value=False),
        )

    def _run(self, customer_id, db_row=None, env_json="{}", default=3, init_db_val=False):
        """
        Helper: runs Config.get_max_followups with fully-mocked DB infrastructure.

        init_db_val controls whether the DB block is entered (True) or bypassed (False).
        When True, session.get() returns db_row (or None for a miss).
        """
        mock_cm = self._make_session_cm(db_row)
        # src.db.database is stubbed by test_retry_config_repository at collection time
        # and registered as a pkg attribute — so patch("src.db.database.init_db") works.
        with patch("src.db.database.init_db", return_value=init_db_val), \
             patch("src.db.database.get_session", return_value=mock_cm), \
             patch("src.db.retry_config_repository.init_db", return_value=init_db_val), \
             patch("src.db.retry_config_repository.get_session", return_value=mock_cm), \
             patch.object(Config, "FOLLOWUP_MAX_BY_CUSTOMER", env_json), \
             patch.object(Config, "FOLLOWUP_DEFAULT_MAX", default):
            return Config.get_max_followups(customer_id)

    def test_db_row_takes_priority(self):
        db_row = MagicMock(retry_count=7)
        result = self._run(customer_id=1001, db_row=db_row, env_json='{"1001": 5}',
                           default=3, init_db_val=True)
        self.assertEqual(result, 7)

    def test_env_var_used_when_no_db_row(self):
        # DB available but no matching row → fall through to env var
        result = self._run(customer_id=202, db_row=None, env_json='{"202": 9}',
                           default=3, init_db_val=True)
        self.assertEqual(result, 9)

    def test_global_default_when_neither_db_nor_env(self):
        result = self._run(customer_id=999, db_row=None, env_json='{}',
                           default=3, init_db_val=True)
        self.assertEqual(result, 3)

    def test_none_customer_id_returns_default(self):
        # customer_id=None skips the DB block entirely
        result = self._run(customer_id=None, db_row=None, env_json='{"": 5}', default=3)
        self.assertEqual(result, 3)

    def test_env_var_ignores_wrong_customer_id(self):
        result = self._run(customer_id=111, db_row=None, env_json='{"222": 9}',
                           default=3, init_db_val=True)
        self.assertEqual(result, 3)

    def test_db_unavailable_falls_back_to_env(self):
        """init_db returns False → DB bypassed → env var hit."""
        result = self._run(customer_id=55, db_row=None, env_json='{"55": 8}',
                           default=3, init_db_val=False)
        self.assertEqual(result, 8)

    def test_followup_default_max_respected(self):
        result = self._run(customer_id=None, db_row=None, env_json='{}',
                           default=5, init_db_val=False)
        self.assertEqual(result, 5)


if __name__ == "__main__":
    unittest.main()
