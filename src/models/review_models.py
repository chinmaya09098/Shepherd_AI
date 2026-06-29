"""
Human review queue models.

Shipments that cannot be auto-processed are queued for human review:
  - Missing fields even after max follow-ups
  - Low extraction confidence
  - Unusual or high-value loads flagged by DecisionAgent
  - Any error in the automated pipeline

Azure Table Storage schema:
  Table:         shepherdreviewqueue
  PartitionKey:  tenant_id
  RowKey:        review_id  (uuid4)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ReviewStatus(str, Enum):
    PENDING    = "pending"
    IN_REVIEW  = "in_review"
    APPROVED   = "approved"
    REJECTED   = "rejected"
    ESCALATED  = "escalated"


class ReviewPriority(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"
    URGENT = "urgent"


def _new_uuid() -> str:
    return str(uuid.uuid4())


class ReviewItem(BaseModel):
    """A shipment queued for human review."""

    # Keys
    review_id: str = Field(default_factory=_new_uuid)   # RowKey
    tenant_id: str = "default"                           # PartitionKey

    # Source email
    message_id: str = ""
    conversation_id: str = ""
    sender_email: str = ""
    subject: str = ""

    # Extracted data (may be partial)
    shipment_data: Optional[Dict[str, Any]] = None
    missing_fields: List[str] = Field(default_factory=list)
    customer_id: Optional[int] = None
    extraction_confidence: Optional[float] = None

    # Review metadata
    status: ReviewStatus = ReviewStatus.PENDING
    priority: ReviewPriority = ReviewPriority.MEDIUM
    reason: str = ""                     # Why it was flagged (e.g. "missing_fields_after_followup")
    assigned_to: Optional[str] = None   # Reviewer email

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None

    # Resolution data filled in by human reviewer
    reviewer_notes: Optional[str] = None
    resolution_data: Optional[Dict[str, Any]] = None

    model_config = {"arbitrary_types_allowed": True}
