"""
Shepherd AI Orchestrator.

The single entry point for processing one email through the full multi-agent
pipeline. Called by:
  - email_processor Azure Function (queue trigger)
  - polling_fallback Azure Function (timer trigger)
  - Streamlit UI (for live demo/testing)

Pipeline:
  1. DedupService.is_processed()                   → skip if already done
  2. ClassificationAgent                            → determine email_type
  3. (if NOT actionable) → early exit
  4. ExtractionAgent                                → OCR + OpenAI extraction
  5. ValidationAgent                                → check required fields
  6. Load ConversationState (if existing thread)    → carry partial data
  7. DecisionAgent                                  → AUTO_APPROVE / FOLLOW_UP / REVIEW
  8a. AUTO_APPROVE: ShipmentCreationAgent → ResponseAgent (confirmation)
  8b. FOLLOW_UP:    ResponseAgent (follow-up) → save ConversationState
  8c. HUMAN_REVIEW: ReviewService.queue() → ResponseAgent (acknowledgment)
  9. DedupService.mark_processed()
 10. Return PipelineResult
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.agents.base_agent import BaseAgent
from src.agents.classification_agent import ClassificationAgent
from src.agents.decision_agent import DecisionAgent
from src.agents.extraction_agent import ExtractionAgent
from src.agents.response_agent import ResponseAgent
from src.agents.shipment_creation_agent import ShipmentCreationAgent
from src.agents.validation_agent import ValidationAgent
from src.models.agent_models import (
    AgentResult,
    AgentStatus,
    AgentTask,
    EmailDecision,
    PipelineResult,
)
from src.models.conversation_models import ConversationState, ConversationStatus
from src.models.review_models import ReviewItem, ReviewPriority
from src.utils.logger import get_logger

logger = get_logger(__name__)


class Orchestrator:
    """
    Chains all agents together and manages cross-cutting concerns
    (dedup, conversation state, review queue, telemetry).
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        graph_client=None,
        access_token: Optional[str] = None,
    ):
        """
        Args:
            connection_string: Azure Storage connection string for state tables
            graph_client:      Initialised GraphClient (for sending replies)
            access_token:      Graph API access token (for sending replies)
        """
        self._conn_str = connection_string
        self._graph_client = graph_client
        self._access_token = access_token

        # Agents
        self._classify = ClassificationAgent()
        self._extract = ExtractionAgent()
        self._validate = ValidationAgent()
        self._decide = DecisionAgent()
        self._respond = ResponseAgent(graph_client=graph_client)
        self._create = ShipmentCreationAgent()

        # Services (lazy — only initialised if connection_string provided)
        self._dedup = None
        self._conv_store = None
        self._review_svc = None

        if connection_string:
            from src.services.dedup_service import DedupService
            from src.services.conversation_store import ConversationStore
            from src.services.review_service import ReviewService
            self._dedup = DedupService(connection_string)
            self._conv_store = ConversationStore(connection_string)
            self._review_svc = ReviewService(connection_string)

    # ── Main entry point ───────────────────────────────────────────────────────

    def process(
        self,
        email_data: Dict[str, Any],
        message_id: str,
        conversation_id: str,
        tenant_id: str = "default",
    ) -> PipelineResult:
        """
        Process one email through the full pipeline.

        Args:
            email_data:      EMLParser-compatible dict (from GraphEmailReader or EMLParser)
            message_id:      Graph message ID
            conversation_id: Outlook conversationId (thread key)
            tenant_id:       Tenant identifier

        Returns:
            PipelineResult with decision, shipments, and agent trace
        """
        correlation_id = str(uuid.uuid4())[:8]
        start = datetime.now(timezone.utc)

        result = PipelineResult(
            message_id=message_id,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            processing_start=start,
        )

        logger.info(
            "Orchestrator.process START correlation_id=%s message_id=%s",
            correlation_id,
            message_id,
        )

        try:
            # ── 1. Dedup check ─────────────────────────────────────────────────
            if self._dedup and self._dedup.is_processed(message_id, tenant_id):
                logger.info(
                    "Skipping duplicate message_id=%s", message_id
                )
                result.decision = EmailDecision.NOT_ACTIONABLE
                result.error = "duplicate"
                return result

            task = AgentTask(
                email_data=email_data,
                message_id=message_id,
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                correlation_id=correlation_id,
            )

            # ── 2. Classification ──────────────────────────────────────────────
            cls_result = self._classify.run(task)
            result.agent_results.append(_result_summary(cls_result))

            if cls_result.status == AgentStatus.FAILED:
                result.error = f"Classification failed: {cls_result.error}"
                return self._finish(result, start)

            email_type = cls_result.data.get("email_type", "other")
            early_decision: Optional[EmailDecision] = cls_result.data.get("early_decision")
            envelope = cls_result.data.get("envelope", {})

            result.email_type = email_type
            task.metadata["envelope"] = envelope

            # Short-circuit for non-actionable / spam
            if early_decision in (EmailDecision.SPAM, EmailDecision.NOT_ACTIONABLE):
                result.decision = early_decision
                if self._dedup:
                    self._dedup.mark_processed(
                        message_id, tenant_id,
                        status=early_decision.value,
                    )
                return self._finish(result, start)

            # ── 3. Extraction ──────────────────────────────────────────────────
            ext_result = self._extract.run(task)
            result.agent_results.append(_result_summary(ext_result))

            if ext_result.status == AgentStatus.FAILED:
                result.error = f"Extraction failed: {ext_result.error}"
                result.decision = EmailDecision.HUMAN_REVIEW
                self._queue_review(result, tenant_id, reason="extraction_failed")
                return self._finish(result, start)

            shipments = ext_result.data.get("shipments", [])
            customer_id = ext_result.data.get("customer_id")
            task.metadata["shipments"] = shipments
            task.metadata["customer_id"] = customer_id
            task.metadata["envelope"] = ext_result.data.get("envelope", envelope)

            result.customer_id = customer_id

            # ── 4. Validation ──────────────────────────────────────────────────
            val_result = self._validate.run(task)
            result.agent_results.append(_result_summary(val_result))

            all_missing = val_result.data.get("all_missing", [])
            task.metadata["all_missing"] = all_missing
            task.metadata["shipments"] = val_result.data.get("shipments", shipments)
            result.missing_fields = all_missing

            # ── 5. Load conversation state ─────────────────────────────────────
            conversation: Optional[ConversationState] = None
            if self._conv_store:
                conversation = self._conv_store.get(conversation_id, tenant_id)
            task.metadata["conversation"] = conversation

            # ── 6. Decision ────────────────────────────────────────────────────
            dec_result = self._decide.run(task)
            result.agent_results.append(_result_summary(dec_result))

            decision: EmailDecision = dec_result.data.get(
                "decision", EmailDecision.HUMAN_REVIEW
            )
            result.decision = decision
            blocking_missing = dec_result.data.get("blocking_missing", [])
            task.metadata["decision"] = decision
            task.metadata["blocking_missing"] = blocking_missing
            task.metadata["access_token"] = self._access_token

            # ── 7a. AUTO_APPROVE ───────────────────────────────────────────────
            if decision == EmailDecision.AUTO_APPROVE:
                create_result = self._create.run(task)
                result.agent_results.append(_result_summary(create_result))
                task.metadata["tms_shipment_ids"] = create_result.data.get("tms_shipment_ids", [])

                resp_result = self._respond.run(task)
                result.agent_results.append(_result_summary(resp_result))
                result.response_sent = resp_result.data.get("sent", False)

                # Clear conversation state if this was a follow-up resolution
                if conversation and self._conv_store:
                    conversation.status = ConversationStatus.COMPLETED
                    self._conv_store.upsert(conversation)

            # ── 7b. FOLLOW_UP ──────────────────────────────────────────────────
            elif decision == EmailDecision.FOLLOW_UP:
                resp_result = self._respond.run(task)
                result.agent_results.append(_result_summary(resp_result))
                result.response_sent = resp_result.data.get("sent", False)
                result.follow_up_fields = blocking_missing

                # Persist partial conversation state
                if self._conv_store:
                    updated_conv = conversation or ConversationState(
                        conversation_id=conversation_id,
                        tenant_id=tenant_id,
                        original_message_id=message_id,
                        sender_email=email_data.get("from", ""),
                        subject=email_data.get("subject", ""),
                    )
                    updated_conv.latest_message_id = message_id
                    updated_conv.missing_fields = blocking_missing
                    updated_conv.follow_up_count += 1
                    updated_conv.customer_id = customer_id
                    # Store partial shipment data
                    if shipments:
                        updated_conv.partial_shipment = (
                            shipments[0].model_dump() if hasattr(shipments[0], "model_dump") else {}
                        )
                    self._conv_store.upsert(updated_conv)

            # ── 7c. HUMAN_REVIEW ───────────────────────────────────────────────
            elif decision == EmailDecision.HUMAN_REVIEW:
                review_reason = dec_result.data.get("reason", "manual_review")
                result.review_reason = review_reason
                self._queue_review(result, tenant_id, reason=review_reason)

                resp_result = self._respond.run(task)
                result.agent_results.append(_result_summary(resp_result))
                result.response_sent = resp_result.data.get("sent", False)

            # ── 8. Mark processed ──────────────────────────────────────────────
            if self._dedup:
                self._dedup.mark_processed(
                    message_id, tenant_id, status=decision.value
                )

            result.shipments = shipments

        except Exception as exc:
            logger.error(
                "Orchestrator unhandled error correlation_id=%s: %s",
                correlation_id,
                exc,
                exc_info=True,
            )
            result.error = str(exc)
            result.decision = EmailDecision.HUMAN_REVIEW

        return self._finish(result, start)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _finish(self, result: PipelineResult, start: datetime) -> PipelineResult:
        result.processing_end = datetime.now(timezone.utc)
        duration = (result.processing_end - start).total_seconds()
        logger.info(
            "Orchestrator.process END correlation_id=%s decision=%s duration=%.2fs",
            result.correlation_id,
            result.decision.value if result.decision else "–",
            duration,
        )
        return result

    def _queue_review(
        self,
        result: PipelineResult,
        tenant_id: str,
        reason: str,
    ) -> None:
        if not self._review_svc:
            return
        try:
            item = ReviewItem(
                tenant_id=tenant_id,
                message_id=result.message_id,
                conversation_id=result.conversation_id,
                subject="",
                missing_fields=result.missing_fields,
                customer_id=result.customer_id,
                reason=reason,
                priority=ReviewPriority.HIGH if "exhausted" in reason else ReviewPriority.MEDIUM,
            )
            self._review_svc.queue(item)
        except Exception as exc:
            logger.error("Failed to queue review item: %s", exc)


def _result_summary(result: AgentResult) -> dict:
    return {
        "agent": result.agent_name,
        "status": result.status.value,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "data_keys": list(result.data.keys()) if result.data else [],
    }
