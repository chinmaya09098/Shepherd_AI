"""
Human-in-the-Loop (HITL) review request model.

Each ReviewRequest represents one extraction that needs manual verification
before it can be completed or forwarded to HyperionTMS.

Status flow:
    pending  →  approved  (reviewer fills/confirms missing fields)
    pending  →  rejected  (reviewer decides email is not actionable)

Review reasons:
    max_retries       — customer did not respond after N follow-up attempts
    send_error        — follow-up email could not be sent (Graph API failure)
    no_customer_match — no HyperionTMS customer could be matched to the sender
    low_confidence    — extraction confidence below the configured threshold

After approval the ``approved_shipment`` dict holds the final, reviewer-verified
shipment payload ready for downstream processing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REVIEW_REASON_MAX_RETRIES    = "max_retries"
REVIEW_REASON_SEND_ERROR     = "send_error"
REVIEW_REASON_NO_CUSTOMER    = "no_customer_match"
REVIEW_REASON_LOW_CONFIDENCE = "low_confidence"

REVIEW_STATUS_PENDING  = "pending"
REVIEW_STATUS_APPROVED = "approved"
REVIEW_STATUS_REJECTED = "rejected"

# Human-readable labels for each reason
REASON_LABELS: Dict[str, str] = {
    REVIEW_REASON_MAX_RETRIES:    "Max follow-ups reached",
    REVIEW_REASON_SEND_ERROR:     "Follow-up send failed",
    REVIEW_REASON_NO_CUSTOMER:    "Customer could not be matched",
    REVIEW_REASON_LOW_CONFIDENCE: "Low extraction confidence",
}


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class ReviewRequest(BaseModel):
    """
    Represents one shipment extraction pending human review.

    Attributes:
        review_id:             Unique UUID for this review.
        conversation_id:       Graph conversationId of the source email thread.
        original_message_id:   Graph message ID of the first email in the thread.
        subject:               Email subject line (display only).
        sender_email:          Sender's email address.
        sender_name:           Sender's display name (optional).
        review_reason:         Why this was routed for review (REVIEW_REASON_* constant).
        flags:                 Additional detail strings explaining the routing decision.
        extraction_confidence: NiceToHaveFields.extraction_confidence from the last
                               extraction (0.0–1.0), or None if not available.
        shipment_data:         Serialised Shipment (model_dump) at the time of routing.
        missing_fields:        Field keys still missing when the review was created.
        followup_count:        Number of follow-up emails already sent.
        status:                "pending" | "approved" | "rejected"
        reviewer_notes:        Free-text notes added by the reviewer.
        approved_shipment:     Reviewer-edited shipment payload (set on approval).
        created_at:            ISO 8601 UTC timestamp when the review was created.
        reviewed_at:           ISO 8601 UTC timestamp when the review was actioned.
    """

    review_id:             str  = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id:       str
    original_message_id:   str
    subject:               str  = ""
    sender_email:          str  = ""
    sender_name:           Optional[str] = None
    review_reason:         str
    flags:                 List[str]           = Field(default_factory=list)
    extraction_confidence: Optional[float]     = None
    shipment_data:         Dict[str, Any]      = Field(default_factory=dict)
    missing_fields:        List[str]           = Field(default_factory=list)
    followup_count:        int                 = 0
    status:                str                 = REVIEW_STATUS_PENDING
    reviewer_notes:        Optional[str]       = None
    approved_shipment:     Optional[Dict[str, Any]] = None
    created_at:            str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    reviewed_at:           Optional[str] = None

    tenant_id:             Optional[str] = None
    """Azure AD tenant ID scoping this review. None = single-tenant / legacy."""

    class Config:
        populate_by_name = True

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def reason_label(self) -> str:
        """Human-readable review reason."""
        return REASON_LABELS.get(self.review_reason, self.review_reason)

    @property
    def is_pending(self) -> bool:
        return self.status == REVIEW_STATUS_PENDING

    def approve(
        self,
        approved_shipment: Dict[str, Any],
        reviewer_notes: str = "",
    ) -> None:
        """Mark this review as approved with the reviewer-verified shipment."""
        self.status            = REVIEW_STATUS_APPROVED
        self.approved_shipment = approved_shipment
        self.reviewer_notes    = reviewer_notes or None
        self.reviewed_at       = datetime.now(timezone.utc).isoformat()

    def reject(self, reviewer_notes: str = "") -> None:
        """Mark this review as rejected."""
        self.status         = REVIEW_STATUS_REJECTED
        self.reviewer_notes = reviewer_notes or None
        self.reviewed_at    = datetime.now(timezone.utc).isoformat()
