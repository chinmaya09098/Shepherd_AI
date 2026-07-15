"""
Conversation state model for thread-aware shipment processing.

Each ConversationState represents the full lifecycle of one email thread —
from the moment a shipment-tender email is first received through extraction,
follow-up cycles, and final completion or escalation.

Supports three reply-correlation strategies beyond conversationId:
  - sender_domain  : same sender email domain
  - normalized_subject : subject with Re:/Fw: stripped
  - reference_ids  : shared shipment/order/PO reference numbers

Lifecycle events are stored inline so the UI can render a timeline without
additional storage lookups.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Lifecycle event constants
# ---------------------------------------------------------------------------
EVENT_EMAIL_INGESTED        = "email_ingested"
EVENT_EMAIL_CLASSIFIED      = "email_classified"
EVENT_EXTRACTION_COMPLETE   = "extraction_complete"
EVENT_CUSTOMER_RESOLVED     = "customer_id_resolved"
EVENT_FOLLOWUP_GENERATED    = "followup_generated"
EVENT_FOLLOWUP_SENT         = "followup_sent"
EVENT_REPLY_RECEIVED        = "reply_received"
EVENT_REPLY_CORRELATED      = "reply_correlated"
EVENT_DATA_MERGED           = "data_merged"
EVENT_VALIDATION_PASSED     = "validation_passed"
EVENT_CONVERSATION_COMPLETE = "conversation_complete"
EVENT_MAX_RETRIES           = "max_retries_reached"
EVENT_ERROR                 = "error"


class ConversationState(BaseModel):
    """
    Persistent state for one shipment email conversation thread.

    Status flow:
        processing          email received, extraction in progress
            ↓
        awaiting_reply      follow-up sent, waiting for customer
            ↓
        complete            all required fields present; shipment ready
        max_retries_reached follow-up limit hit; needs manual review
        failed              unrecoverable error
    """

    # ── Thread identity ───────────────────────────────────────────────────
    conversation_id: str
    """Microsoft Graph conversationId — stable across the entire thread."""

    original_message_id: str
    """Graph message ID of the customer's first email."""

    last_message_id: str
    """Graph message ID of the most recent message we should reply to."""

    # ── Sender metadata (primary + fallback correlation) ──────────────────
    sender_email: str
    sender_name: Optional[str] = None
    sender_domain: str = ""
    """Domain part of sender_email, e.g. 'ecogistics.org'. Used for sender-based fallback."""

    # ── Subject (fallback correlation) ────────────────────────────────────
    subject: str
    normalized_subject: str = ""
    """Subject with Re:/Fw:/[External] stripped and lowercased. Used for subject-similarity fallback."""

    # ── Reference IDs (fallback correlation) ─────────────────────────────
    reference_ids: List[str] = Field(default_factory=list)
    """Shipment IDs, order numbers, PO numbers extracted from the email.
    Used for reference-ID-based fallback correlation."""

    # ── Extraction state ──────────────────────────────────────────────────
    partial_shipment: Dict[str, Any] = Field(default_factory=dict)
    """Serialised Shipment.model_dump(by_alias=True, mode='json'). Grows as replies arrive."""

    missing_fields: List[str] = Field(default_factory=list)
    """Field keys still missing after the last extraction / merge."""

    # ── Customer identity ─────────────────────────────────────────────────
    customer_id: Optional[int] = None
    """Resolved HyperionTMS customerId — drives per-customer max_followups."""

    # ── Follow-up tracking ────────────────────────────────────────────────
    followup_count: int = 0
    max_followups: int = 3

    # ── Status ────────────────────────────────────────────────────────────
    status: str = "processing"

    # ── Lifecycle events (inline audit trail) ─────────────────────────────
    lifecycle_events: List[Dict[str, Any]] = Field(default_factory=list)
    """
    Chronological list of processing events. Each entry:
      {
        "event":     str,          # one of the EVENT_* constants above
        "timestamp": str,          # ISO 8601 UTC
        "details":   dict          # event-specific payload
      }
    """

    # ── Timestamps ────────────────────────────────────────────────────────
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_followup_at: Optional[str] = None

    class Config:
        populate_by_name = True

    # ── Helpers ───────────────────────────────────────────────────────────

    def add_event(self, event: str, details: Optional[Dict[str, Any]] = None) -> None:
        """
        Append a lifecycle event and bump last_updated_at.

        Args:
            event:   One of the EVENT_* module-level constants.
            details: Optional dict of event-specific metadata.
        """
        self.lifecycle_events.append(
            {
                "event": event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "details": details or {},
            }
        )
        self.last_updated_at = datetime.now(timezone.utc).isoformat()

    def event_summary(self) -> List[str]:
        """
        Return a compact list of human-readable event lines for UI display.
        Format: "[HH:MM UTC] event_name — key: value, ..."
        """
        lines: List[str] = []
        for ev in self.lifecycle_events:
            ts = ev.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
                ts_fmt = dt.strftime("%b %d %H:%M UTC")
            except Exception:
                ts_fmt = ts[:16]
            event_name = ev.get("event", "unknown").replace("_", " ").title()
            details = ev.get("details", {})
            detail_str = ", ".join(f"{k}: {v}" for k, v in details.items() if v)
            lines.append(f"**{ts_fmt}** — {event_name}" + (f"  ·  {detail_str}" if detail_str else ""))
        return lines
