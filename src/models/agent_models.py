"""
Agent models — typed state containers for the multi-agent pipeline.

Each agent receives an AgentTask and returns an AgentResult.
The Orchestrator chains these together and tracks overall state in PipelineResult.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"


class EmailDecision(str, Enum):
    AUTO_APPROVE   = "auto_approve"    # All fields present, confidence high — create shipment
    FOLLOW_UP      = "follow_up"       # Missing fields — send follow-up email to customer
    HUMAN_REVIEW   = "human_review"    # Ambiguous or low-confidence — queue for review
    SPAM           = "spam"            # Irrelevant email — discard
    NOT_ACTIONABLE = "not_actionable"  # Not a shipment (tracking, status update, etc.)


@dataclass
class AgentTask:
    """Input passed to every agent in the pipeline."""
    email_data: Dict[str, Any]              # EMLParser-compatible email dict
    message_id: str                          # Graph message ID
    conversation_id: str                     # Outlook conversation/thread ID
    tenant_id: str = "default"              # Which tenant's mailbox
    correlation_id: str = ""                # Trace ID for this pipeline run
    metadata: Dict[str, Any] = field(default_factory=dict)   # Carry-forward data between agents


@dataclass
class AgentResult:
    """Output produced by every agent in the pipeline."""
    agent_name: str
    status: AgentStatus
    data: Dict[str, Any] = field(default_factory=dict)   # Structured result payload
    error: Optional[str] = None
    duration_ms: Optional[float] = None


class PipelineResult(BaseModel):
    """Final result of the complete agent pipeline for one email."""
    message_id: str
    conversation_id: str
    correlation_id: str = ""
    email_type: str = "unknown"
    decision: EmailDecision = EmailDecision.NOT_ACTIONABLE
    shipments: List[Any] = Field(default_factory=list)   # List[Shipment] — Any to avoid circular
    missing_fields: List[str] = Field(default_factory=list)
    customer_id: Optional[int] = None
    response_sent: bool = False
    follow_up_fields: List[str] = Field(default_factory=list)
    review_reason: Optional[str] = None
    agent_results: List[Dict[str, Any]] = Field(default_factory=list)
    processing_start: Optional[datetime] = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    processing_end: Optional[datetime] = None
    error: Optional[str] = None

    model_config = {"arbitrary_types_allowed": True}
