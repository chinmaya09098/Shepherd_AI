"""
Tests for FollowupOrchestrator.send_timeout_followup

Covers all return paths:
  1. followup_sent    — awaiting_reply, below max, Graph send succeeds
  2. no_action_needed — conversation not in awaiting_reply status
  3. max_retries      — followup_count >= max_followups
  4. error (graph)    — Graph send_reply returns False
  5. error (corrupt)  — partial_shipment cannot be parsed by Shipment.model_validate
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Stub out modules with heavy Azure / network / DB dependencies so the test
# can run without any live credentials.
# ---------------------------------------------------------------------------

def _make_stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


# src.utils.logger — just needs get_logger()
sys.modules.setdefault(
    "src.utils.logger",
    _make_stub("src.utils.logger", get_logger=lambda _: MagicMock()),
)

# src.secrets — loaded by src.config at import time
sys.modules.setdefault(
    "src.secrets",
    _make_stub("src.secrets", load_secrets_from_keyvault=lambda: None),
)

# src.config — needs to exist; provide minimal Config with get_max_followups
import os
os.environ.setdefault("FOLLOWUP_DEFAULT_MAX", "3")
os.environ.setdefault("FOLLOWUP_MAX_BY_CUSTOMER", "{}")

_config_mod = _make_stub("src.config")

class _FakeConfig:
    FOLLOWUP_DEFAULT_MAX = 3
    FOLLOWUP_MAX_BY_CUSTOMER = "{}"

    @staticmethod
    def get_max_followups(customer_id=None):
        return 3

_config_mod.Config = _FakeConfig
sys.modules.setdefault("src.config", _config_mod)

# src.services.conversation_tracker
_tracker_mod = _make_stub(
    "src.services.conversation_tracker",
    ConversationTracker=MagicMock,
    CorrelationResult=MagicMock,
    _normalize_subject=lambda s: s.lower(),
)
sys.modules.setdefault("src.services.conversation_tracker", _tracker_mod)

# src.services.conversation_lifecycle_logger
sys.modules.setdefault(
    "src.services.conversation_lifecycle_logger",
    _make_stub("src.services.conversation_lifecycle_logger", ConversationLifecycleLogger=MagicMock),
)

# src.services.followup_email_generator
sys.modules.setdefault(
    "src.services.followup_email_generator",
    _make_stub("src.services.followup_email_generator", FollowupEmailGenerator=MagicMock),
)

# src.services.graph_client
sys.modules.setdefault(
    "src.services.graph_client",
    _make_stub("src.services.graph_client", GraphClient=MagicMock),
)

# src.extractors.reply_merger
sys.modules.setdefault(
    "src.extractors.reply_merger",
    _make_stub("src.extractors.reply_merger", ReplyMerger=MagicMock),
)

# src.models.graph_models
sys.modules.setdefault(
    "src.models.graph_models",
    _make_stub("src.models.graph_models", MailMessage=MagicMock),
)

# ---------------------------------------------------------------------------
# NOW import the real modules under test (after stubs are in place)
# ---------------------------------------------------------------------------
from src.models.conversation_state import ConversationState  # noqa: E402
from src.models.shipment import Shipment                     # noqa: E402
from src.services.followup_orchestrator import (             # noqa: E402
    FollowupOrchestrator,
    FollowupResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal valid partial_shipment that Shipment.model_validate can parse
_PARTIAL_SHIPMENT = {
    "emailType": "shipment_tender",
    "missingRequiredFields": ["pickupLocation", "dropLocation"],
    "requiredFields": {
        "customerName": "ACME Corp",
        "pickupLocation": None,
        "dropLocation": None,
        "pickupDate": None,
        "pickupWindow": None,
        "deliveryDate": None,
        "equipmentMode": "FTL",
        "items": [],
        "totalWeight": None,
        "accessorials": [],
    },
    "niceToHaveFields": {},
}


def _make_state(**overrides) -> ConversationState:
    """Return a minimal ConversationState in awaiting_reply status."""
    defaults = dict(
        conversation_id     = "conv-test-1234567890",
        original_message_id = "msg-orig-001",
        last_message_id     = "msg-last-001",
        sender_email        = "customer@acme.com",
        sender_name         = "Alice",
        sender_domain       = "acme.com",
        subject             = "Load Tender – Chicago to Dallas",
        normalized_subject  = "load tender – chicago to dallas",
        followup_count      = 1,
        max_followups       = 3,
        status              = "awaiting_reply",
        missing_fields      = ["pickupLocation", "dropLocation"],
        partial_shipment    = _PARTIAL_SHIPMENT,
    )
    defaults.update(overrides)
    return ConversationState(**defaults)


def _make_orchestrator() -> FollowupOrchestrator:
    """Return an orchestrator with all internal collaborators mocked."""
    orc = FollowupOrchestrator()
    orc._tracker   = MagicMock()
    orc._tracker.save = MagicMock()
    orc._lifecycle = MagicMock()
    orc._lifecycle.log = MagicMock()
    orc._email_gen = MagicMock()
    orc._email_gen.generate_followup_email = MagicMock(
        return_value="<html>Please provide missing fields.</html>"
    )
    return orc


def _mock_graph(send_reply_result: bool = True) -> MagicMock:
    """Return a mock GraphClient with an async send_reply."""
    gc = MagicMock()
    gc.send_reply = AsyncMock(return_value=send_reply_result)
    return gc


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestSendTimeoutFollowup(unittest.TestCase):

    # ── Case 1: Happy path — reminder sent successfully ────────────────────

    def test_followup_sent_happy_path(self):
        """
        awaiting_reply, followup_count < max_followups, Graph succeeds
        → action=followup_sent, count incremented, state saved
        """
        orc   = _make_orchestrator()
        state = _make_state(followup_count=1, max_followups=3)
        gc    = _mock_graph(send_reply_result=True)

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "followup_sent")
        self.assertEqual(result.followup_count, 2)           # 1 + 1
        self.assertIn("pickupLocation", result.missing_fields)
        self.assertEqual(result.conversation_id, "conv-test-1234567890")
        # State should be persisted
        orc._tracker.save.assert_called_once_with(state)
        # followup_count on the state object should be bumped
        self.assertEqual(state.followup_count, 2)
        # last_followup_at should be stamped
        self.assertIsNotNone(state.last_followup_at)
        # Graph reply must target last_message_id
        gc.send_reply.assert_awaited_once()
        call_kwargs = gc.send_reply.await_args.kwargs
        self.assertEqual(call_kwargs["message_id"], "msg-last-001")
        self.assertTrue(call_kwargs["reply_html"])

    def test_followup_sent_first_reminder(self):
        """
        followup_count=0 (never reminded before) → followup_count becomes 1.
        """
        orc   = _make_orchestrator()
        state = _make_state(followup_count=0, max_followups=3)
        gc    = _mock_graph(send_reply_result=True)

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "followup_sent")
        self.assertEqual(result.followup_count, 1)
        self.assertEqual(state.followup_count, 1)

    def test_followup_sent_uses_correct_missing_fields(self):
        """
        The email generator receives only the blocking fields (non-warning-only).
        """
        orc   = _make_orchestrator()
        # "items" is warning-only and should be filtered out
        state = _make_state(missing_fields=["pickupLocation", "items"])
        gc    = _mock_graph(send_reply_result=True)

        _run(orc.send_timeout_followup(state, gc, "tok"))

        call_args = orc._email_gen.generate_followup_email.call_args
        passed_fields = call_args.kwargs.get("missing_fields") or call_args.args[0]
        self.assertIn("pickupLocation", passed_fields)
        self.assertNotIn("items", passed_fields)   # warning-only — filtered

    def test_followup_sent_lifecycle_events_logged(self):
        """
        Both FOLLOWUP_GENERATED and FOLLOWUP_SENT lifecycle events must be emitted.
        """
        orc   = _make_orchestrator()
        state = _make_state()
        gc    = _mock_graph(send_reply_result=True)

        _run(orc.send_timeout_followup(state, gc, "tok"))

        logged_events = [call.args[1] for call in orc._lifecycle.log.call_args_list]
        self.assertIn("followup_generated", logged_events)
        self.assertIn("followup_sent",      logged_events)

    # ── Case 2: Wrong status — not awaiting_reply ──────────────────────────

    def test_no_action_when_status_complete(self):
        """
        status='complete' → no_action_needed, no email sent, no state save.
        """
        orc   = _make_orchestrator()
        state = _make_state(status="complete")
        gc    = _mock_graph()

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "no_action_needed")
        gc.send_reply.assert_not_awaited()
        orc._tracker.save.assert_not_called()

    def test_no_action_when_status_processing(self):
        """status='processing' → no_action_needed."""
        orc   = _make_orchestrator()
        state = _make_state(status="processing")
        gc    = _mock_graph()

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "no_action_needed")
        gc.send_reply.assert_not_awaited()

    def test_no_action_when_status_max_retries_reached(self):
        """status='max_retries_reached' → no_action_needed."""
        orc   = _make_orchestrator()
        state = _make_state(status="max_retries_reached")
        gc    = _mock_graph()

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "no_action_needed")
        gc.send_reply.assert_not_awaited()

    def test_no_action_when_status_expired(self):
        """status='expired' → no_action_needed."""
        orc   = _make_orchestrator()
        state = _make_state(status="expired")
        gc    = _mock_graph()

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "no_action_needed")

    # ── Case 3: Max retries boundary ──────────────────────────────────────

    def test_max_retries_when_count_equals_max(self):
        """
        followup_count == max_followups → max_retries, status updated to
        max_retries_reached, state saved, no email sent.
        """
        orc   = _make_orchestrator()
        state = _make_state(followup_count=3, max_followups=3)
        gc    = _mock_graph()

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "max_retries")
        self.assertEqual(state.status, "max_retries_reached")
        gc.send_reply.assert_not_awaited()
        orc._tracker.save.assert_called_once_with(state)

    def test_max_retries_when_count_exceeds_max(self):
        """followup_count > max_followups (defensive) → also max_retries."""
        orc   = _make_orchestrator()
        state = _make_state(followup_count=5, max_followups=3)
        gc    = _mock_graph()

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "max_retries")
        self.assertEqual(state.status, "max_retries_reached")

    def test_max_retries_count_and_message_in_result(self):
        """Result contains followup_count and a human-readable message."""
        orc   = _make_orchestrator()
        state = _make_state(followup_count=3, max_followups=3)
        gc    = _mock_graph()

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.followup_count, 3)
        self.assertIn("3", result.message)            # max count mentioned

    def test_last_valid_followup_before_max(self):
        """
        followup_count = max_followups - 1 → still sends (not yet at limit).
        """
        orc   = _make_orchestrator()
        state = _make_state(followup_count=2, max_followups=3)
        gc    = _mock_graph(send_reply_result=True)

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "followup_sent")
        self.assertEqual(result.followup_count, 3)

    # ── Case 4: Graph send_reply failure ──────────────────────────────────

    def test_error_when_graph_send_fails(self):
        """
        send_reply returns False → action=error, state saved with ERROR event,
        followup_count NOT incremented.
        """
        orc   = _make_orchestrator()
        state = _make_state(followup_count=1, max_followups=3)
        gc    = _mock_graph(send_reply_result=False)

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "error")
        # followup_count must NOT advance on failure
        self.assertEqual(state.followup_count, 1)
        # State must still be saved (with error event)
        orc._tracker.save.assert_called_once_with(state)
        # An ERROR lifecycle event must be emitted
        logged_events = [call.args[1] for call in orc._lifecycle.log.call_args_list]
        self.assertIn("error", logged_events)

    def test_error_graph_fail_returns_message(self):
        """Error result includes a descriptive message."""
        orc   = _make_orchestrator()
        state = _make_state()
        gc    = _mock_graph(send_reply_result=False)

        result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertTrue(result.message)   # non-empty message

    # ── Case 5: Corrupt partial_shipment ──────────────────────────────────

    def test_error_when_partial_shipment_corrupt(self):
        """
        Shipment.model_validate raises → action=error, no Graph call.
        """
        orc   = _make_orchestrator()
        state = _make_state(partial_shipment={"invalid": "garbage"})
        gc    = _mock_graph()

        # patch Shipment.model_validate to raise
        with patch(
            "src.services.followup_orchestrator.Shipment.model_validate",
            side_effect=ValueError("bad schema"),
        ):
            result = _run(orc.send_timeout_followup(state, gc, "tok"))

        self.assertEqual(result.action, "error")
        self.assertIn("bad schema", result.message)
        gc.send_reply.assert_not_awaited()
        orc._tracker.save.assert_not_called()

    # ── Lifecycle event content verification ──────────────────────────────

    def test_followup_sent_event_has_timeout_trigger(self):
        """
        Events emitted for a timeout reminder carry 'trigger': 'timeout'
        so they can be distinguished from reply-driven follow-ups in audit logs.
        """
        orc   = _make_orchestrator()
        state = _make_state()
        gc    = _mock_graph(send_reply_result=True)

        _run(orc.send_timeout_followup(state, gc, "tok"))

        # Inspect inline lifecycle_events on the ConversationState object
        timeout_events = [
            ev for ev in state.lifecycle_events
            if ev.get("details", {}).get("trigger") == "timeout"
        ]
        self.assertGreaterEqual(len(timeout_events), 2,
            "Expected at least followup_generated and followup_sent with trigger=timeout")

    def test_state_last_followup_at_updated_on_success(self):
        """last_followup_at is stamped with a recent UTC timestamp on success."""
        orc   = _make_orchestrator()
        state = _make_state()
        gc    = _mock_graph(send_reply_result=True)

        before = datetime.now(timezone.utc)
        _run(orc.send_timeout_followup(state, gc, "tok"))
        after  = datetime.now(timezone.utc)

        self.assertIsNotNone(state.last_followup_at)
        stamped = datetime.fromisoformat(state.last_followup_at)
        self.assertGreaterEqual(stamped, before)
        self.assertLessEqual(stamped, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
