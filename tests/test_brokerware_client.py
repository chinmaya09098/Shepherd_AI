"""
Tests for src/services/brokerware_client.py

Covers:
  - _match_one_email: plain email, RFC 2822 "Display Name <addr>", no match, ambiguous domain
  - _load_tenant: shepherd, shepherdwest, and unmapped (default) routing
  - is_configured: with and without credentials
  - match_customer: sender-first then receiver fallback
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy dependencies so we can import brokerware_client without a live
# Azure / network / DB environment.
# ---------------------------------------------------------------------------

def _make_stub(name, **attrs):
    import types
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


# src.utils.logger
_logger_stub = _make_stub("src.utils.logger", get_logger=lambda _: MagicMock())
sys.modules.setdefault("src.utils.logger", _logger_stub)

# src.models.shipment
_shipment_stub = _make_stub("src.models.shipment", Shipment=MagicMock)
sys.modules.setdefault("src.models.shipment", _shipment_stub)

# src.models.client_format
_fmt_stub = _make_stub("src.models.client_format", format_client_json=MagicMock(return_value={}))
sys.modules.setdefault("src.models.client_format", _fmt_stub)

# src.secrets  (imported by src.config)
sys.modules.setdefault("src.secrets", _make_stub("src.secrets", load_secrets_from_keyvault=lambda: None))

# We need src.config to be the real one; set env vars first so its class attrs
# are initialised with known values.
os.environ.setdefault("BROKERWARE_BASE_URL",      "https://shepherd.brokerware.io")
os.environ.setdefault("BROKERWARE_CLIENT_ID",     "default-client-id")
os.environ.setdefault("BROKERWARE_CLIENT_SECRET", "default-secret")

_TENANT_MAP = json.dumps({
    "shepherd@3plsystems0.onmicrosoft.com":  "shepherd",
    "shepherd1@3plsystems0.onmicrosoft.com": "shepherd",
    "shepherd2@3plsystems0.onmicrosoft.com": "shepherdwest",
})
os.environ.setdefault("BROKERWARE_MAILBOX_TENANT_MAP", _TENANT_MAP)
os.environ.setdefault("BROKERWARE_SHEPHERD_BASE_URL",      "https://shepherd.brokerware.io")
os.environ.setdefault("BROKERWARE_SHEPHERD_CLIENT_ID",     "shepherd-client-id")
os.environ.setdefault("BROKERWARE_SHEPHERD_CLIENT_SECRET", "shepherd-secret")
os.environ.setdefault("BROKERWARE_SHEPHERDWEST_BASE_URL",      "https://shepherdwest.brokerware.io")
os.environ.setdefault("BROKERWARE_SHEPHERDWEST_CLIENT_ID",     "shepherdwest-client-id")
os.environ.setdefault("BROKERWARE_SHEPHERDWEST_CLIENT_SECRET", "shepherdwest-secret")

# Now import the module under test (Config will load the env vars above)
from src.services.brokerware_client import (  # noqa: E402
    _match_one_email,
    _load_tenant,
    _to_match,
    is_configured,
    match_customer,
    _tenant_contacts_cache,
    _BrokerwareTenant,
)
from src.config import Config  # noqa: E402


# ---------------------------------------------------------------------------
# Sample contacts fixture
# ---------------------------------------------------------------------------

CONTACTS = [
    {"customerId": 1001, "clientId": 10, "email": "jack3pl@outlook.com"},
    {"customerId": 1002, "clientId": 11, "email": "alice@acme.com"},
    {"customerId": 1003, "clientId": 12, "email": "bob@acme.com"},   # same domain, diff customer
    {"customerId": 1004, "clientId": 13, "email": "sole@unique-corp.com"},
]


# ===========================================================================
# _match_one_email
# ===========================================================================

class TestMatchOneEmail(unittest.TestCase):

    def test_plain_email_exact_match(self):
        """Exact email address returns confident match."""
        result = _match_one_email(CONTACTS, "jack3pl@outlook.com")
        self.assertIsNotNone(result)
        matches, confident = result
        self.assertTrue(confident)
        self.assertEqual(matches[0]["customerId"], 1001)

    def test_rfc2822_display_name_stripped(self):
        """'Display Name <addr>' format: only the addr part is matched."""
        result = _match_one_email(CONTACTS, "Jack Nguyen <jack3pl@outlook.com>")
        self.assertIsNotNone(result)
        matches, confident = result
        self.assertTrue(confident)
        self.assertEqual(matches[0]["customerId"], 1001)

    def test_case_insensitive(self):
        """Email matching is case-insensitive."""
        result = _match_one_email(CONTACTS, "JACK3PL@OUTLOOK.COM")
        self.assertIsNotNone(result)
        matches, _ = result
        self.assertEqual(matches[0]["customerId"], 1001)

    def test_no_match_returns_none(self):
        """Unknown email with no contacts → None."""
        result = _match_one_email(CONTACTS, "nobody@nowhere.com")
        self.assertIsNone(result)

    def test_empty_email_returns_none(self):
        """Empty string → None (don't crash)."""
        result = _match_one_email(CONTACTS, "")
        self.assertIsNone(result)

    def test_ambiguous_domain_returns_none(self):
        """Domain maps to multiple customers → None (safe, not a guess)."""
        # acme.com has both 1002 and 1003
        result = _match_one_email(CONTACTS, "unknown@acme.com")
        self.assertIsNone(result)

    def test_single_customer_domain_match(self):
        """Domain maps to exactly one customer → confident match."""
        result = _match_one_email(CONTACTS, "any.user@unique-corp.com")
        self.assertIsNotNone(result)
        matches, confident = result
        self.assertTrue(confident)
        self.assertEqual(matches[0]["customerId"], 1004)

    def test_empty_contacts_list(self):
        """Empty contacts list → None."""
        result = _match_one_email([], "jack3pl@outlook.com")
        self.assertIsNone(result)

    def test_rfc2822_no_display_name(self):
        """'<addr>' format without display name still extracts address."""
        result = _match_one_email(CONTACTS, "<jack3pl@outlook.com>")
        # parseaddr("<addr>") returns ("", "jack3pl@outlook.com") which is fine
        self.assertIsNotNone(result)
        matches, confident = result
        self.assertEqual(matches[0]["customerId"], 1001)


# ===========================================================================
# _load_tenant
# ===========================================================================

class TestLoadTenant(unittest.TestCase):

    def _patch_map(self, mapping_json: str):
        return patch.object(Config, "BROKERWARE_MAILBOX_TENANT_MAP", mapping_json)

    def test_shepherd_mailbox_routes_to_shepherd_tenant(self):
        with self._patch_map(_TENANT_MAP):
            tenant = _load_tenant("shepherd@3plsystems0.onmicrosoft.com")
        self.assertEqual(tenant.key, "shepherd")
        self.assertIn("shepherd.brokerware.io", tenant.base_url)

    def test_shepherd1_also_routes_to_shepherd_tenant(self):
        with self._patch_map(_TENANT_MAP):
            tenant = _load_tenant("shepherd1@3plsystems0.onmicrosoft.com")
        self.assertEqual(tenant.key, "shepherd")

    def test_shepherd2_routes_to_shepherdwest_tenant(self):
        with self._patch_map(_TENANT_MAP):
            tenant = _load_tenant("shepherd2@3plsystems0.onmicrosoft.com")
        self.assertEqual(tenant.key, "shepherdwest")
        self.assertIn("shepherdwest.brokerware.io", tenant.base_url)

    def test_unmapped_mailbox_returns_default_tenant(self):
        with self._patch_map(_TENANT_MAP):
            tenant = _load_tenant("unknown@example.com")
        self.assertEqual(tenant.key, "default")

    def test_empty_upn_returns_default_tenant(self):
        with self._patch_map(_TENANT_MAP):
            tenant = _load_tenant("")
        self.assertEqual(tenant.key, "default")

    def test_case_insensitive_upn_lookup(self):
        """Mailbox UPN is matched case-insensitively."""
        with self._patch_map(_TENANT_MAP):
            tenant = _load_tenant("SHEPHERD2@3PLSYSTEMS0.ONMICROSOFT.COM")
        self.assertEqual(tenant.key, "shepherdwest")

    def test_malformed_tenant_map_falls_back_to_default(self):
        """A bad JSON tenant map doesn't crash — returns default tenant."""
        with self._patch_map("THIS IS NOT JSON"):
            tenant = _load_tenant("shepherd@3plsystems0.onmicrosoft.com")
        self.assertEqual(tenant.key, "default")


# ===========================================================================
# is_configured
# ===========================================================================

class TestIsConfigured(unittest.TestCase):

    def test_configured_when_credentials_present(self):
        with self._patch_map(_TENANT_MAP):
            result = is_configured("shepherd@3plsystems0.onmicrosoft.com")
        self.assertTrue(result)

    def test_not_configured_when_client_id_missing(self):
        # Patch both the per-tenant env var and the fallback Config attribute so
        # _load_tenant genuinely returns an empty client_id.
        with patch.dict(os.environ, {"BROKERWARE_SHEPHERD_CLIENT_ID": ""}), \
             patch.object(Config, "BROKERWARE_CLIENT_ID", None), \
             self._patch_map(_TENANT_MAP):
            result = is_configured("shepherd@3plsystems0.onmicrosoft.com")
        self.assertFalse(result)

    def _patch_map(self, mapping_json: str):
        return patch.object(Config, "BROKERWARE_MAILBOX_TENANT_MAP", mapping_json)


# ===========================================================================
# match_customer
# ===========================================================================

class TestMatchCustomer(unittest.TestCase):

    def _run_match(self, contacts, sender, receiver="", mailbox_upn=""):
        target = "src.services.brokerware_client._get_contacts_cached"
        with patch(target, return_value=contacts):
            return match_customer(sender, receiver, mailbox_upn)

    def test_sender_matched_first(self):
        result = self._run_match(CONTACTS, sender="jack3pl@outlook.com")
        self.assertTrue(result["is_broker_match"])
        self.assertEqual(result["matches"][0]["customerId"], 1001)

    def test_receiver_used_as_fallback(self):
        result = self._run_match(
            CONTACTS,
            sender="nobody@nowhere.com",
            receiver="jack3pl@outlook.com",
        )
        self.assertTrue(result["is_broker_match"])
        self.assertEqual(result["matches"][0]["customerId"], 1001)

    def test_display_name_sender_still_matches(self):
        result = self._run_match(CONTACTS, sender="Jack Nguyen <jack3pl@outlook.com>")
        self.assertTrue(result["is_broker_match"])
        self.assertEqual(result["matches"][0]["customerId"], 1001)

    def test_no_match_returns_empty(self):
        result = self._run_match(CONTACTS, sender="ghost@void.io", receiver="also@void.io")
        self.assertFalse(result["is_broker_match"])
        self.assertEqual(result["matches"], [])

    def test_empty_contacts_returns_no_match(self):
        result = self._run_match([], sender="jack3pl@outlook.com")
        self.assertFalse(result["is_broker_match"])
        self.assertEqual(result["matches"], [])

    def test_ambiguous_domain_sender_falls_back_to_receiver(self):
        """Sender has ambiguous domain (acme.com); receiver is unique → receiver wins."""
        result = self._run_match(
            CONTACTS,
            sender="new-user@acme.com",
            receiver="sole@unique-corp.com",
        )
        self.assertTrue(result["is_broker_match"])
        self.assertEqual(result["matches"][0]["customerId"], 1004)


# ===========================================================================
# _to_match helper
# ===========================================================================

class TestToMatch(unittest.TestCase):

    def test_basic_shape(self):
        contact = {"customerId": 42, "clientId": 7, "email": "test@example.com"}
        m = _to_match(contact)
        self.assertEqual(m["customerId"], 42)
        self.assertEqual(m["clientId"], 7)
        self.assertEqual(m["email"], "test@example.com")
        self.assertEqual(m["emailDomain"], "example.com")
        self.assertIsNone(m["customerName"])

    def test_missing_email(self):
        contact = {"customerId": 1, "clientId": 2}
        m = _to_match(contact)
        self.assertEqual(m["email"], "")
        self.assertEqual(m["emailDomain"], "")


if __name__ == "__main__":
    unittest.main()
