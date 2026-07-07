"""
Human-in-the-Loop (HITL) routing service.

Decides whether a shipment extraction should be escalated to a human reviewer
and creates a ReviewRequest when it does.

Routing triggers (checked in priority order):
  1. max_retries       — FollowupResult.action == "max_retries"
  2. send_error        — FollowupResult.action == "error" + missing required fields
  3. no_customer_match — customer_id is None on a shipment_tender
  4. low_confidence    — extraction_confidence < CONFIDENCE_THRESHOLD + missing fields

Usage::

    from src.services.hitl_router import HITLRouter

    router   = HITLRouter()
    decision = router.evaluate(
        shipment=shipment,
        followup_result=followup_result,
        customer_id=resolved_customer_id,
    )

    if decision.should_review:
        review_req = router.route(
            shipment=shipment,
            message=mail_message,
            decision=decision,
            conversation_id=conv_id,
        )
        # review_req is already persisted in the ReviewQueue
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from src.models.graph_models import MailMessage
from src.models.review_request import (
    REVIEW_REASON_LOW_CONFIDENCE,
    REVIEW_REASON_MAX_RETRIES,
    REVIEW_REASON_NO_CUSTOMER,
    REVIEW_REASON_SEND_ERROR,
    ReviewRequest,
)
from src.models.shipment import Shipment
from src.services.review_queue import ReviewQueue
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Extraction confidence below this value triggers a review (when combined with
# missing required fields).
CONFIDENCE_THRESHOLD: float = 0.50


# ---------------------------------------------------------------------------
# RoutingDecision
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    """
    Result of ``HITLRouter.evaluate()``.

    Attributes:
        should_review: True when the extraction should be escalated.
        reason:        Primary REVIEW_REASON_* constant.
        flags:         List of human-readable detail strings explaining the decision.
    """
    should_review: bool        = False
    reason:        str         = ""
    flags:         List[str]   = field(default_factory=list)


# ---------------------------------------------------------------------------
# HITLRouter
# ---------------------------------------------------------------------------

class HITLRouter:
    """
    Evaluates whether a shipment extraction needs human review and, if so,
    creates and persists a ReviewRequest.
    """

    def __init__(self):
        self._queue = ReviewQueue()

    # ── Decision ──────────────────────────────────────────────────────────────

    def evaluate(
        self,
        shipment: Shipment,
        followup_result=None,   # Optional[FollowupResult] — avoid circular import
        customer_id: Optional[int] = None,
    ) -> RoutingDecision:
        """
        Determine whether this extraction should be routed for human review.

        Checks four triggers in priority order and returns on the first match.

        Args:
            shipment:       The extracted (possibly partial) Shipment.
            followup_result: FollowupResult from FollowupOrchestrator, or None.
            customer_id:    Resolved HyperionTMS customer ID, or None.

        Returns:
            RoutingDecision with ``should_review``, ``reason``, and ``flags``.
        """
        missing = shipment.missing_required_fields or []

        # ── Trigger 1: max retries reached ────────────────────────────────────
        if followup_result and getattr(followup_result, "action", "") == "max_retries":
            count = getattr(followup_result, "followup_count", 0)
            flags = [
                f"{count} follow-up email(s) sent with no complete reply",
                f"Still missing: {', '.join(missing)}" if missing else "Fields still unresolved",
            ]
            logger.info("HITL trigger: max_retries (followup_count=%d)", count)
            return RoutingDecision(
                should_review=True,
                reason=REVIEW_REASON_MAX_RETRIES,
                flags=flags,
            )

        # ── Trigger 2: follow-up send error + missing fields ──────────────────
        if (
            followup_result
            and getattr(followup_result, "action", "") == "error"
            and missing
        ):
            err_msg = getattr(followup_result, "message", "Unknown error")
            flags = [
                f"Graph API error: {err_msg}",
                f"Still missing: {', '.join(missing)}",
            ]
            logger.info("HITL trigger: send_error — %s", err_msg)
            return RoutingDecision(
                should_review=True,
                reason=REVIEW_REASON_SEND_ERROR,
                flags=flags,
            )

        # ── Trigger 3: customer could not be matched ───────────────────────────
        if (
            customer_id is None
            and shipment.email_type == "shipment_tender"
        ):
            sender_hint = ""
            flags = ["No HyperionTMS customer matched to sender email/domain"]
            if sender_hint:
                flags.append(f"Sender: {sender_hint}")
            logger.info("HITL trigger: no_customer_match")
            return RoutingDecision(
                should_review=True,
                reason=REVIEW_REASON_NO_CUSTOMER,
                flags=flags,
            )

        # ── Trigger 4: low extraction confidence + missing fields ──────────────
        confidence = (
            shipment.nice_to_have_fields.extraction_confidence
            if shipment.nice_to_have_fields
            else None
        )
        if (
            confidence is not None
            and confidence < CONFIDENCE_THRESHOLD
            and missing
        ):
            flags = [
                f"Extraction confidence {confidence:.0%} below threshold {CONFIDENCE_THRESHOLD:.0%}",
                f"Missing required fields: {', '.join(missing)}",
            ]
            logger.info(
                "HITL trigger: low_confidence (confidence=%.2f threshold=%.2f)",
                confidence, CONFIDENCE_THRESHOLD,
            )
            return RoutingDecision(
                should_review=True,
                reason=REVIEW_REASON_LOW_CONFIDENCE,
                flags=flags,
            )

        return RoutingDecision(should_review=False)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(
        self,
        shipment: Shipment,
        message: MailMessage,
        decision: RoutingDecision,
        conversation_id: str = "",
        followup_count: int  = 0,
    ) -> ReviewRequest:
        """
        Create a ReviewRequest and persist it to the ReviewQueue.

        Args:
            shipment:         The shipment extraction to review.
            message:          The originating MailMessage (for display metadata).
            decision:         RoutingDecision produced by ``evaluate()``.
            conversation_id:  Graph conversationId of the email thread.
            followup_count:   Number of follow-up emails already sent.

        Returns:
            The persisted ReviewRequest.
        """
        sender_email = ""
        sender_name  = None
        if message.from_ and message.from_.email_address:
            sender_email = message.from_.email_address.address or ""
            sender_name  = message.from_.email_address.name

        confidence = (
            shipment.nice_to_have_fields.extraction_confidence
            if shipment.nice_to_have_fields
            else None
        )

        req = ReviewRequest(
            conversation_id=conversation_id or (message.conversation_id or ""),
            original_message_id=message.id or "",
            subject=message.subject or "",
            sender_email=sender_email,
            sender_name=sender_name,
            review_reason=decision.reason,
            flags=decision.flags,
            extraction_confidence=confidence,
            shipment_data=shipment.model_dump(by_alias=True, mode="json"),
            missing_fields=list(shipment.missing_required_fields or []),
            followup_count=followup_count,
        )

        self._queue.submit(req)
        logger.info(
            "Routed to HITL review: review_id=%s reason=%s conv=%s",
            req.review_id[:8],
            req.review_reason,
            req.conversation_id[:20] if req.conversation_id else "—",
        )
        return req

    # ── Convenience ───────────────────────────────────────────────────────────

    def evaluate_and_route(
        self,
        shipment: Shipment,
        message: MailMessage,
        conversation_id: str = "",
        followup_result=None,
        customer_id: Optional[int] = None,
        followup_count: int = 0,
    ) -> Optional[ReviewRequest]:
        """
        Evaluate and, if warranted, immediately route to the review queue.

        Returns the ReviewRequest if routed, None otherwise.
        """
        decision = self.evaluate(
            shipment=shipment,
            followup_result=followup_result,
            customer_id=customer_id,
        )
        if decision.should_review:
            return self.route(
                shipment=shipment,
                message=message,
                decision=decision,
                conversation_id=conversation_id,
                followup_count=followup_count,
            )
        return None
