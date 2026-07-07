"""
Follow-up orchestrator — coordinates the automated email follow-up workflow
with full thread-aware conversation management.

Responsibilities:
  1. Register every shipment-tender email in ConversationState from first touch.
  2. Evaluate extracted Shipments for blocking missing required fields.
  3. Generate and send follow-up emails via GraphClient.send_reply().
  4. Correlate customer replies using four fallback strategies
     (conversationId → reference IDs → subject similarity → sender).
  5. Merge reply data iteratively until all fields are present.
  6. Log every lifecycle transition to ConversationLifecycleLogger.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from src.extractors.reply_merger import ReplyMerger
from src.models.conversation_state import (
    ConversationState,
    EVENT_EMAIL_INGESTED,
    EVENT_EXTRACTION_COMPLETE,
    EVENT_FOLLOWUP_GENERATED,
    EVENT_FOLLOWUP_SENT,
    EVENT_REPLY_RECEIVED,
    EVENT_REPLY_CORRELATED,
    EVENT_DATA_MERGED,
    EVENT_VALIDATION_PASSED,
    EVENT_CONVERSATION_COMPLETE,
    EVENT_MAX_RETRIES,
    EVENT_ERROR,
)
from src.models.graph_models import MailMessage
from src.models.shipment import Shipment
from src.services.conversation_lifecycle_logger import ConversationLifecycleLogger
from src.services.conversation_tracker import ConversationTracker, CorrelationResult, _normalize_subject
from src.services.followup_email_generator import FollowupEmailGenerator
from src.services.graph_client import GraphClient
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Fields that are informational-only and do not block shipment creation
_WARNING_ONLY_FIELDS = {"items"}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class FollowupResult:
    """
    Outcome of one orchestration call.

    action values:
        followup_sent      — follow-up email was generated and sent
        complete           — all required fields are now present
        max_retries        — follow-up limit reached; manual review needed
        no_action_needed   — no blocking fields missing; nothing to do
        error              — error prevented sending the email
    """
    action:        str
    shipment:      Optional[Shipment]   = None
    missing_fields: List[str]           = field(default_factory=list)
    followup_html: Optional[str]        = None
    followup_count: int                 = 0
    message:       str                  = ""
    conversation_id: Optional[str]      = None
    correlation:   Optional[CorrelationResult] = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class FollowupOrchestrator:
    """
    Thread-aware follow-up workflow orchestrator.

    All async methods must be awaited; call them via streamlit_app.run_async()
    in the Streamlit context.
    """

    def __init__(self) -> None:
        self._tracker   = ConversationTracker()
        self._email_gen = FollowupEmailGenerator()
        self._merger    = ReplyMerger()
        self._lifecycle = ConversationLifecycleLogger()

    # ── Public API — initial extraction ──────────────────────────────────

    async def handle_initial_extraction(
        self,
        shipment:     Shipment,
        message:      MailMessage,
        graph_client: GraphClient,
        access_token: str,
    ) -> FollowupResult:
        """
        Called after extracting a shipment from a customer's email.

        Always registers the conversation in ConversationState (even if no
        follow-up is needed) so the full lifecycle is tracked from first touch.

        If blocking required fields are missing, generates and sends a
        follow-up email as a reply in the same thread.
        """
        conv_id      = message.conversation_id or message.id or "unknown"
        msg_id       = message.id or ""
        sender_email = _sender_email(message)
        sender_name  = _sender_name(message)
        sender_domain = sender_email.split("@")[-1] if "@" in sender_email else ""
        subject      = message.subject or "Your Shipment Request"
        norm_subject = _normalize_subject(subject)
        ref_ids      = _extract_reference_ids(shipment)
        blocking     = self._blocking(shipment.missing_required_fields)

        # ── Register conversation (first-touch tracking) ──────────────────
        state = ConversationState(
            conversation_id   = conv_id,
            original_message_id = msg_id,
            last_message_id   = msg_id,
            sender_email      = sender_email,
            sender_name       = sender_name,
            sender_domain     = sender_domain,
            subject           = subject,
            normalized_subject = norm_subject,
            reference_ids     = ref_ids,
            partial_shipment  = shipment.model_dump(by_alias=True, mode="json"),
            missing_fields    = shipment.missing_required_fields,
            followup_count    = 0,
            status            = "processing",
        )
        state.add_event(
            EVENT_EMAIL_INGESTED,
            {"message_id": msg_id, "sender": sender_email, "subject": subject},
        )
        state.add_event(
            EVENT_EXTRACTION_COMPLETE,
            {
                "missing_fields": shipment.missing_required_fields,
                "blocking_missing": blocking,
                "reference_ids": ref_ids,
            },
        )
        self._lifecycle.log(conv_id, EVENT_EMAIL_INGESTED, {"sender": sender_email})
        self._lifecycle.log(conv_id, EVENT_EXTRACTION_COMPLETE, {"blocking_missing": blocking})

        # ── No blocking fields missing ────────────────────────────────────
        if not blocking:
            state.status = "complete"
            state.add_event(EVENT_VALIDATION_PASSED, {"missing_fields": shipment.missing_required_fields})
            state.add_event(EVENT_CONVERSATION_COMPLETE, {"reason": "all_fields_present_on_first_extraction"})
            self._tracker.save(state)
            self._lifecycle.log(conv_id, EVENT_CONVERSATION_COMPLETE)
            logger.info("No blocking fields missing — conversation %s complete on first extraction", conv_id[:20])
            return FollowupResult(
                action="no_action_needed",
                shipment=shipment,
                missing_fields=shipment.missing_required_fields,
                conversation_id=conv_id,
                message="Shipment is complete — no follow-up required.",
            )

        # ── Generate follow-up email ──────────────────────────────────────
        logger.info("Generating follow-up #1 for %s — missing: %s", conv_id[:20], blocking)

        followup_html = self._email_gen.generate_followup_email(
            missing_fields=blocking,
            shipment=shipment,
            original_subject=subject,
            sender_name=sender_name,
            followup_number=1,
        )
        state.add_event(EVENT_FOLLOWUP_GENERATED, {"missing_fields": blocking})
        self._lifecycle.log(conv_id, EVENT_FOLLOWUP_GENERATED, {"missing_fields": blocking})

        success = await graph_client.send_reply(
            access_token=access_token,
            message_id=msg_id,
            reply_body=followup_html,
            reply_html=True,
        )

        if not success:
            state.status = "failed"
            state.add_event(EVENT_ERROR, {"reason": "graph_reply_failed", "message_id": msg_id})
            self._tracker.save(state)
            self._lifecycle.log(conv_id, EVENT_ERROR, {"reason": "graph_reply_failed"})
            logger.error("Failed to send follow-up for message %s", msg_id)
            return FollowupResult(
                action="error",
                shipment=shipment,
                missing_fields=blocking,
                conversation_id=conv_id,
                message="Graph API reply failed. Check access token and Mail.Send permission.",
            )

        state.followup_count    = 1
        state.status            = "awaiting_reply"
        state.last_followup_at  = datetime.now(timezone.utc).isoformat()
        state.add_event(
            EVENT_FOLLOWUP_SENT,
            {"followup_number": 1, "missing_fields": blocking},
        )
        self._tracker.save(state)
        self._lifecycle.log(conv_id, EVENT_FOLLOWUP_SENT, {"followup_number": 1, "missing_fields": blocking})

        logger.info("Follow-up #1 sent for %s — missing: %s", conv_id[:20], blocking)
        return FollowupResult(
            action="followup_sent",
            shipment=shipment,
            missing_fields=blocking,
            followup_html=followup_html,
            followup_count=1,
            conversation_id=conv_id,
            message=(
                f"Follow-up email sent requesting: {', '.join(blocking)}. "
                "Conversation state saved — reply will be tracked automatically."
            ),
        )

    # ── Public API — customer reply ───────────────────────────────────────

    async def handle_reply(
        self,
        reply_message:  MailMessage,
        reply_shipment: Shipment,
        graph_client:   GraphClient,
        access_token:   str,
        correlation:    Optional[CorrelationResult] = None,
    ) -> FollowupResult:
        """
        Called when a customer reply is detected in a tracked conversation.

        If no CorrelationResult is supplied, performs correlation internally
        using all four fallback strategies.
        """
        # Resolve correlation if not provided by caller
        if correlation is None:
            ref_ids     = _extract_reference_ids(reply_shipment)
            correlation = self._tracker.correlate_message(reply_message, ref_ids)

        if not correlation.state:
            return FollowupResult(
                action="error",
                message="Could not correlate reply to any tracked conversation.",
            )

        state = correlation.state
        conv_id = state.conversation_id

        if state.status != "awaiting_reply":
            return FollowupResult(
                action="no_action_needed",
                conversation_id=conv_id,
                message=f"Conversation already in status '{state.status}' — no action taken.",
            )

        # Log reply received
        state.add_event(
            EVENT_REPLY_RECEIVED,
            {
                "message_id": reply_message.id or "",
                "sender": _sender_email(reply_message),
            },
        )
        state.add_event(
            EVENT_REPLY_CORRELATED,
            {
                "method": correlation.method,
                "confidence": round(correlation.confidence, 2),
            },
        )
        self._lifecycle.log(conv_id, EVENT_REPLY_RECEIVED, {"message_id": reply_message.id or ""})
        self._lifecycle.log(
            conv_id,
            EVENT_REPLY_CORRELATED,
            {"method": correlation.method, "confidence": round(correlation.confidence, 2)},
        )

        # Reconstruct partial shipment and merge with reply
        existing = Shipment.model_validate(state.partial_shipment)
        merged   = self._merger.merge(existing, reply_shipment)
        remaining_blocking = self._blocking(merged.missing_required_fields)

        state.partial_shipment  = merged.model_dump(by_alias=True, mode="json")
        state.missing_fields    = merged.missing_required_fields
        state.last_message_id   = reply_message.id or state.last_message_id
        state.add_event(
            EVENT_DATA_MERGED,
            {
                "fields_resolved": list(set(state.missing_fields) - set(remaining_blocking)),
                "still_missing": remaining_blocking,
            },
        )
        self._lifecycle.log(conv_id, EVENT_DATA_MERGED, {"still_missing": remaining_blocking})

        # ── Complete ──────────────────────────────────────────────────────
        if not remaining_blocking:
            state.status = "complete"
            state.add_event(EVENT_VALIDATION_PASSED)
            state.add_event(EVENT_CONVERSATION_COMPLETE, {"followup_count": state.followup_count})
            self._tracker.save(state)
            self._lifecycle.log(conv_id, EVENT_CONVERSATION_COMPLETE)
            logger.info("Conversation %s complete after reply (followup_count=%d)", conv_id[:20], state.followup_count)
            return FollowupResult(
                action="complete",
                shipment=merged,
                missing_fields=merged.missing_required_fields,
                followup_count=state.followup_count,
                conversation_id=conv_id,
                correlation=correlation,
                message="All required fields are now present. Shipment is ready for creation.",
            )

        # ── Max retries ───────────────────────────────────────────────────
        if state.followup_count >= state.max_followups:
            state.status = "max_retries_reached"
            state.add_event(EVENT_MAX_RETRIES, {"followup_count": state.followup_count, "still_missing": remaining_blocking})
            self._tracker.save(state)
            self._lifecycle.log(conv_id, EVENT_MAX_RETRIES, {"still_missing": remaining_blocking})
            logger.warning("Max follow-ups (%d) reached for %s", state.max_followups, conv_id[:20])
            return FollowupResult(
                action="max_retries",
                shipment=merged,
                missing_fields=remaining_blocking,
                followup_count=state.followup_count,
                conversation_id=conv_id,
                correlation=correlation,
                message=f"Maximum of {state.max_followups} follow-up(s) reached. Manual review required.",
            )

        # ── Send next follow-up ───────────────────────────────────────────
        next_count = state.followup_count + 1
        followup_html = self._email_gen.generate_followup_email(
            missing_fields=remaining_blocking,
            shipment=merged,
            original_subject=state.subject,
            sender_name=state.sender_name,
            followup_number=next_count,
        )
        state.add_event(EVENT_FOLLOWUP_GENERATED, {"followup_number": next_count, "missing_fields": remaining_blocking})
        self._lifecycle.log(conv_id, EVENT_FOLLOWUP_GENERATED, {"followup_number": next_count})

        success = await graph_client.send_reply(
            access_token=access_token,
            message_id=state.last_message_id,
            reply_body=followup_html,
            reply_html=True,
        )

        if not success:
            state.status = "failed"
            state.add_event(EVENT_ERROR, {"reason": "graph_reply_failed", "followup_number": next_count})
            self._tracker.save(state)
            self._lifecycle.log(conv_id, EVENT_ERROR, {"reason": "graph_reply_failed"})
            return FollowupResult(
                action="error",
                shipment=merged,
                missing_fields=remaining_blocking,
                conversation_id=conv_id,
                message=f"Failed to send follow-up #{next_count}.",
            )

        state.followup_count   = next_count
        state.last_followup_at = datetime.now(timezone.utc).isoformat()
        state.add_event(EVENT_FOLLOWUP_SENT, {"followup_number": next_count, "missing_fields": remaining_blocking})
        self._tracker.save(state)
        self._lifecycle.log(conv_id, EVENT_FOLLOWUP_SENT, {"followup_number": next_count, "missing_fields": remaining_blocking})

        logger.info("Follow-up #%d sent for %s — still missing: %s", next_count, conv_id[:20], remaining_blocking)
        return FollowupResult(
            action="followup_sent",
            shipment=merged,
            missing_fields=remaining_blocking,
            followup_html=followup_html,
            followup_count=next_count,
            conversation_id=conv_id,
            correlation=correlation,
            message=f"Follow-up #{next_count} sent. Still waiting for: {', '.join(remaining_blocking)}",
        )

    # ── UI helpers ────────────────────────────────────────────────────────

    def correlate_message(
        self,
        message:       MailMessage,
        reference_ids: Optional[List[str]] = None,
    ) -> CorrelationResult:
        """
        Correlate an incoming message to a tracked conversation using all
        four fallback strategies. Exposes ConversationTracker.correlate_message.
        """
        return self._tracker.correlate_message(message, reference_ids or [])

    def is_tracked_reply(self, message: MailMessage) -> bool:
        """
        Quick boolean check — True if the message is a customer reply in a
        tracked conversation (backward-compatible wrapper).
        """
        result = self.correlate_message(message)
        return result.is_reply

    def get_conversation_state(self, conversation_id: Optional[str]) -> Optional[ConversationState]:
        if not conversation_id:
            return None
        return self._tracker.load(conversation_id)

    def list_active_conversations(self) -> List[ConversationState]:
        return self._tracker.list_active()

    # ── Private helpers ───────────────────────────────────────────────────

    @staticmethod
    def _blocking(missing: List[str]) -> List[str]:
        return [f for f in missing if f not in _WARNING_ONLY_FIELDS]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _sender_email(message: MailMessage) -> str:
    if message.from_ and message.from_.email_address:
        return message.from_.email_address.address or ""
    return ""


def _sender_name(message: MailMessage) -> Optional[str]:
    if message.from_ and message.from_.email_address:
        name = message.from_.email_address.name
        if name:
            return name.split()[0] if " " in name else name
    return None


def _extract_reference_ids(shipment: Shipment) -> List[str]:
    """Collect all reference identifiers from a Shipment for correlation."""
    ntf  = shipment.nice_to_have_fields
    refs = []
    if ntf.shipment_id:    refs.append(str(ntf.shipment_id))
    if ntf.order_number:   refs.append(str(ntf.order_number))
    if ntf.purchase_order: refs.append(str(ntf.purchase_order))
    if ntf.tracking_number: refs.append(str(ntf.tracking_number))
    refs.extend(str(r) for r in (ntf.reference_numbers or []) if r)
    # deduplicate, preserve order
    seen: set = set()
    unique = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique
