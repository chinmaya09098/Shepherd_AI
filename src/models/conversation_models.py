"""
Conversation state models for multi-turn email threads.

When Shepherd sends a follow-up email requesting missing fields, it stores
the partial extraction here. When the customer replies, the system loads
this state, applies the new data, and continues the pipeline.

Azure Table Storage schema:
  Table:         shepherdconversations
  PartitionKey:  tenant_id
  RowKey:        conversation_id  (Outlook conversationId)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConversationStatus(str, Enum):
    ACTIVE    = "active"      # Awaiting customer reply
    COMPLETED = "completed"   # All data collected, shipment created
    EXPIRED   = "expired"     # Follow-up timed out (> 7 days)
    CANCELLED = "cancelled"   # Manually cancelled


class ConversationState(BaseModel):
    """Persistent conversation state stored in Azure Table Storage."""

    # Keys
    conversation_id: str                                         # RowKey
    tenant_id: str = "default"                                  # PartitionKey

    # Status
    status: ConversationStatus = ConversationStatus.ACTIVE

    # Email context
    original_message_id: str = ""     # First email that started the conversation
    latest_message_id: str = ""       # Most recent email in the thread
    sender_email: str = ""
    subject: str = ""

    # Extraction state
    email_type: str = "shipment_tender"
    partial_shipment: Optional[Dict[str, Any]] = None    # JSON-serialisable partial data
    missing_fields: List[str] = Field(default_factory=list)
    customer_id: Optional[int] = None

    # Follow-up tracking
    follow_up_count: int = 0
    max_follow_ups: int = 3
    last_follow_up_sent: Optional[datetime] = None

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=7)
    )

    model_config = {"arbitrary_types_allowed": True}

    def is_expired(self) -> bool:
        """True if the conversation has passed its expiry time."""
        return datetime.now(timezone.utc) > self.expires_at

    def can_send_follow_up(self) -> bool:
        """True if another follow-up email is allowed."""
        return (
            self.status == ConversationStatus.ACTIVE
            and self.follow_up_count < self.max_follow_ups
            and not self.is_expired()
        )
