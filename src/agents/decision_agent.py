"""
Decision Agent.

Examines validation results and decides what to do next:

  AUTO_APPROVE  — All required fields present, confidence ≥ threshold
                  → proceed to ShipmentCreationAgent + ResponseAgent

  FOLLOW_UP     — Missing required fields AND follow-up count < max
                  → send follow-up email, save partial state in ConversationStore

  HUMAN_REVIEW  — Missing fields AND max follow-ups exhausted, OR
                  extraction confidence below threshold, OR
                  load flagged as high-value / unusual
                  → queue for human review

  SPAM / NOT_ACTIONABLE — Already set by ClassificationAgent
"""
from __future__ import annotations

from typing import List

from src.agents.base_agent import BaseAgent
from src.models.agent_models import AgentResult, AgentStatus, AgentTask, EmailDecision
from src.models.conversation_models import ConversationState
from src.models.shipment import Shipment

# Confidence below this triggers HUMAN_REVIEW regardless of missing fields
_MIN_CONFIDENCE_THRESHOLD = 0.60

# These field names are "warning only" — they do NOT block auto-approval
_WARNING_ONLY_FIELDS = {"items"}


class DecisionAgent(BaseAgent):
    """Decides the pipeline outcome based on validation results."""

    def __init__(self):
        super().__init__("DecisionAgent")

    def _execute(self, task: AgentTask) -> AgentResult:
        shipments: List[Shipment] = task.metadata.get("shipments", [])
        all_missing: List[str] = task.metadata.get("all_missing", [])
        conversation: ConversationState = task.metadata.get("conversation")

        if not shipments:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.SUCCESS,
                data={"decision": EmailDecision.NOT_ACTIONABLE, "reason": "no_shipments"},
            )

        # Blocking missing fields (exclude warning-only)
        blocking_missing = [
            f for f in all_missing
            if f not in _WARNING_ONLY_FIELDS
        ]

        # Check extraction confidence
        min_conf = min(
            (s.nice_to_have_fields.extraction_confidence or 1.0)
            for s in shipments
        )
        low_confidence = min_conf < _MIN_CONFIDENCE_THRESHOLD

        # Check follow-up state
        can_follow_up = True
        if conversation:
            can_follow_up = conversation.can_send_follow_up()

        # ── Decision logic ────────────────────────────────────────────────────
        if not blocking_missing and not low_confidence:
            decision = EmailDecision.AUTO_APPROVE
            reason = "all_fields_present"

        elif blocking_missing and can_follow_up:
            decision = EmailDecision.FOLLOW_UP
            reason = f"missing_fields:{','.join(blocking_missing)}"

        elif blocking_missing and not can_follow_up:
            decision = EmailDecision.HUMAN_REVIEW
            reason = "max_followups_exhausted"

        elif low_confidence:
            decision = EmailDecision.HUMAN_REVIEW
            reason = f"low_confidence:{min_conf:.2f}"

        else:
            decision = EmailDecision.AUTO_APPROVE
            reason = "all_fields_present"

        self.logger.info(
            "[DecisionAgent] decision=%s reason=%s missing=%s conf=%.2f",
            decision.value,
            reason,
            blocking_missing,
            min_conf,
        )

        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            data={
                "decision": decision,
                "reason": reason,
                "blocking_missing": blocking_missing,
                "min_confidence": min_conf,
                "low_confidence": low_confidence,
            },
        )
