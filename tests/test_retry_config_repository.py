"""
Tests for src/db/retry_config_repository.py

Covers:
  - get_retry_count : DB hit, DB miss (uses default), customer_id=None, DB unavailable
  - upsert_retry_configs : empty rows, DB unavailable, normal upsert
  - sync_from_brokerware : no contacts, de-duplication of duplicate customer_ids
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub src.secrets before src.config is imported
# ---------------------------------------------------------------------------
sys.modules.setdefault(
    "src.secrets",
    types.SimpleNamespace(load_secrets_from_keyvault=lambda: None),
)

# ---------------------------------------------------------------------------
# Stub src.db.models — avoids triggering SQLAlchemy ORM / DB setup entirely.
# CustomerRetryConfig is used only as a lookup key (session.get(Model, pk))
# which we mock in each test anyway.
# ---------------------------------------------------------------------------
_models_stub = types.ModuleType("src.db.models")
_models_stub.CustomerRetryConfig = MagicMock()
_models_stub.Base = MagicMock()
_models_stub.EmailRecord = MagicMock()
sys.modules["src.db.models"] = _models_stub

# ---------------------------------------------------------------------------
# Stub src.db.database — avoids engine creation / DB connection.
# Each test patches init_db / get_session in the repository's own namespace.
# ---------------------------------------------------------------------------
_db_stub = types.ModuleType("src.db.database")
_db_stub.init_db = MagicMock(return_value=False)
_db_stub.get_session = MagicMock()
sys.modules["src.db.database"] = _db_stub

# Register stubs as package attributes so patch("src.db.database.X") resolves correctly.
import src.db as _src_db_pkg  # noqa: E402
_src_db_pkg.database = _db_stub
_src_db_pkg.models = _models_stub

# Now import the module under test — it will use the stubs above.
from src.db.retry_config_repository import (  # noqa: E402
    get_retry_count,
    upsert_retry_configs,
    sync_from_brokerware,
    _brokerware_domain,
)
from src.config import Config  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================

def _mock_session(row=None, rowcount=1):
    """Return a mock session context-manager whose .get() returns *row*."""
    session = MagicMock()
    session.get.return_value = row
    session.execute.return_value = MagicMock(rowcount=rowcount)
    cm = MagicMock(
        __enter__=lambda s: session,
        __exit__=MagicMock(return_value=False),
    )
    return cm, session


# ===========================================================================
# _brokerware_domain
# ===========================================================================

class TestBrokerwareDomain(unittest.TestCase):

    def test_extracts_hostname(self):
        with patch.object(Config, "BROKERWARE_BASE_URL", "https://shepherd.brokerware.io"):
            domain = _brokerware_domain()
        self.assertEqual(domain, "shepherd.brokerware.io")

    def test_url_without_scheme(self):
        with patch.object(Config, "BROKERWARE_BASE_URL", "shepherd.brokerware.io"):
            domain = _brokerware_domain()
        self.assertEqual(domain, "shepherd.brokerware.io")


# ===========================================================================
# get_retry_count
# ===========================================================================

class TestGetRetryCount(unittest.TestCase):

    def test_returns_row_retry_count_on_db_hit(self):
        row = MagicMock(retry_count=7)
        cm, _ = _mock_session(row=row)
        with patch("src.db.retry_config_repository.init_db", return_value=True), \
             patch("src.db.retry_config_repository.get_session", return_value=cm):
            result = get_retry_count(1001)
        self.assertEqual(result, 7)

    def test_returns_default_on_db_miss(self):
        cm, _ = _mock_session(row=None)
        with patch("src.db.retry_config_repository.init_db", return_value=True), \
             patch("src.db.retry_config_repository.get_session", return_value=cm), \
             patch.object(Config, "FOLLOWUP_DEFAULT_MAX", 3):
            result = get_retry_count(9999)
        self.assertEqual(result, 3)

    def test_returns_default_when_customer_id_is_none(self):
        with patch.object(Config, "FOLLOWUP_DEFAULT_MAX", 3):
            result = get_retry_count(None)
        self.assertEqual(result, 3)

    def test_returns_default_when_db_unavailable(self):
        with patch("src.db.retry_config_repository.init_db", return_value=False), \
             patch.object(Config, "FOLLOWUP_DEFAULT_MAX", 3):
            result = get_retry_count(42)
        self.assertEqual(result, 3)

    def test_returns_default_on_db_exception(self):
        cm, session = _mock_session()
        session.get.side_effect = Exception("connection lost")
        with patch("src.db.retry_config_repository.init_db", return_value=True), \
             patch("src.db.retry_config_repository.get_session", return_value=cm), \
             patch.object(Config, "FOLLOWUP_DEFAULT_MAX", 3):
            result = get_retry_count(1)
        self.assertEqual(result, 3)

    def test_custom_default_max_respected(self):
        cm, _ = _mock_session(row=None)
        with patch("src.db.retry_config_repository.init_db", return_value=True), \
             patch("src.db.retry_config_repository.get_session", return_value=cm), \
             patch.object(Config, "FOLLOWUP_DEFAULT_MAX", 10):
            result = get_retry_count(999)
        self.assertEqual(result, 10)


# ===========================================================================
# upsert_retry_configs
# ===========================================================================

class TestUpsertRetryConfigs(unittest.TestCase):

    def test_empty_rows_returns_zero_without_db_call(self):
        with patch("src.db.retry_config_repository.init_db", return_value=True) as mock_init:
            result = upsert_retry_configs([])
        self.assertEqual(result, 0)
        mock_init.assert_not_called()

    def test_db_unavailable_returns_zero(self):
        rows = [{"customer_id": 1, "client_id": 10, "domain": "x.io", "retry_count": 3}]
        with patch("src.db.retry_config_repository.init_db", return_value=False):
            result = upsert_retry_configs(rows)
        self.assertEqual(result, 0)

    def test_upsert_returns_rowcount(self):
        rows = [
            {"customer_id": 1, "client_id": 10, "domain": "x.io", "retry_count": 3},
            {"customer_id": 2, "client_id": 11, "domain": "x.io", "retry_count": 3},
        ]
        cm, session = _mock_session(rowcount=2)
        mock_stmt = MagicMock()
        mock_stmt.excluded = MagicMock()
        mock_stmt.on_conflict_do_update.return_value = mock_stmt
        with patch("src.db.retry_config_repository.init_db", return_value=True), \
             patch("src.db.retry_config_repository.get_session", return_value=cm), \
             patch("src.db.retry_config_repository.pg_insert", return_value=mock_stmt):
            result = upsert_retry_configs(rows)
        self.assertEqual(result, 2)

    def test_db_exception_returns_zero(self):
        rows = [{"customer_id": 1, "client_id": 10, "domain": "x.io", "retry_count": 3}]
        cm, session = _mock_session()
        session.execute.side_effect = Exception("DB write error")
        mock_stmt = MagicMock()
        mock_stmt.excluded = MagicMock()
        mock_stmt.on_conflict_do_update.return_value = mock_stmt
        with patch("src.db.retry_config_repository.init_db", return_value=True), \
             patch("src.db.retry_config_repository.get_session", return_value=cm), \
             patch("src.db.retry_config_repository.pg_insert", return_value=mock_stmt):
            result = upsert_retry_configs(rows)
        self.assertEqual(result, 0)


# ===========================================================================
# sync_from_brokerware
# ===========================================================================

class TestSyncFromBrokerware(unittest.TestCase):
    # sync_from_brokerware does a lazy import inside the function body:
    #   from src.services.brokerware_client import get_customer_contacts
    # so we must patch at the source module, NOT at the repository's namespace.
    _GCC_PATCH = "src.services.brokerware_client.get_customer_contacts"

    def test_no_contacts_returns_zero(self):
        with patch(self._GCC_PATCH, return_value=[]), \
             patch.object(Config, "BROKERWARE_BASE_URL", "https://shepherd.brokerware.io"):
            result = sync_from_brokerware()
        self.assertEqual(result, 0)

    def test_deduplicates_by_customer_id(self):
        """Two contacts sharing a customerId must produce only one DB row."""
        contacts = [
            {"customerId": 1001, "clientId": 10, "email": "a@x.com"},
            {"customerId": 1001, "clientId": 10, "email": "b@x.com"},  # duplicate
            {"customerId": 1002, "clientId": 11, "email": "c@x.com"},
        ]
        with patch(self._GCC_PATCH, return_value=contacts), \
             patch.object(Config, "BROKERWARE_BASE_URL", "https://shepherd.brokerware.io"), \
             patch("src.db.retry_config_repository.upsert_retry_configs", return_value=2) as mock_up:
            result = sync_from_brokerware()

        self.assertEqual(result, 2)
        called_rows = mock_up.call_args[0][0]
        self.assertEqual(len(called_rows), 2)
        customer_ids = {r["customer_id"] for r in called_rows}
        self.assertEqual(customer_ids, {1001, 1002})

    def test_default_retry_count_used(self):
        """default_retry_count param overrides Config.FOLLOWUP_DEFAULT_MAX."""
        contacts = [{"customerId": 50, "clientId": 5, "email": "z@x.com"}]
        with patch(self._GCC_PATCH, return_value=contacts), \
             patch.object(Config, "BROKERWARE_BASE_URL", "https://shepherd.brokerware.io"), \
             patch("src.db.retry_config_repository.upsert_retry_configs", return_value=1) as mock_up:
            sync_from_brokerware(default_retry_count=7)

        called_rows = mock_up.call_args[0][0]
        self.assertEqual(called_rows[0]["retry_count"], 7)

    def test_none_customer_id_skipped(self):
        """Contacts without customerId must be skipped."""
        contacts = [
            {"customerId": None, "clientId": 10, "email": "no-id@x.com"},
            {"customerId": 2000, "clientId": 20, "email": "has-id@x.com"},
        ]
        with patch(self._GCC_PATCH, return_value=contacts), \
             patch.object(Config, "BROKERWARE_BASE_URL", "https://shepherd.brokerware.io"), \
             patch("src.db.retry_config_repository.upsert_retry_configs", return_value=1) as mock_up:
            sync_from_brokerware()

        called_rows = mock_up.call_args[0][0]
        self.assertEqual(len(called_rows), 1)
        self.assertEqual(called_rows[0]["customer_id"], 2000)

    def test_domain_extracted_from_base_url(self):
        contacts = [{"customerId": 100, "clientId": 1, "email": "x@x.com"}]
        with patch(self._GCC_PATCH, return_value=contacts), \
             patch.object(Config, "BROKERWARE_BASE_URL", "https://shepherd.brokerware.io"), \
             patch("src.db.retry_config_repository.upsert_retry_configs", return_value=1) as mock_up:
            sync_from_brokerware()

        called_rows = mock_up.call_args[0][0]
        self.assertEqual(called_rows[0]["domain"], "shepherd.brokerware.io")


if __name__ == "__main__":
    unittest.main()
