"""
Classification Agent.

Determines the email type using OpenAI (via the existing OpenAIAgent envelope
extractor). Produces one of:
  shipment_tender | shipment_quote | tracking_request | status_update | spam | other

If email_type is anything other than shipment_tender, the pipeline short-circuits
with EmailDecision.NOT_ACTIONABLE (or SPAM for spam).
"""
from __future__ import annotations

from src.agents.base_agent import BaseAgent
from src.extractors.openai_agent import OpenAIAgent
from src.models.agent_models import AgentResult, AgentStatus, AgentTask, EmailDecision


_ACTIONABLE_TYPES = {"shipment_tender", "shipment_quote"}
_SPAM_TYPES = {"spam"}


class ClassificationAgent(BaseAgent):
    """Classifies incoming email and determines whether to continue the pipeline."""

    def __init__(self):
        super().__init__("ClassificationAgent")
        self._openai = OpenAIAgent()

    def _execute(self, task: AgentTask) -> AgentResult:
        email_data = task.email_data
        body = email_data.get("body", "")
        attachments = email_data.get("attachments", [])

        # Build a light attachment summary for the envelope call
        att_summary = "\n\n".join(
            f"[{a['filename']}]" for a in attachments[:5]
        ) if attachments else ""

        # Use the existing envelope extractor — it classifies the email type
        try:
            envelope = self._openai.extract_email_envelope(
                email_body=body,
                all_attachment_texts=att_summary,
            )
        except Exception as exc:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                error=str(exc),
            )

        email_type = envelope.get("emailType", "other")
        is_actionable = email_type in _ACTIONABLE_TYPES

        if email_type in _SPAM_TYPES:
            decision = EmailDecision.SPAM
        elif is_actionable:
            decision = None          # Will be set by DecisionAgent
        else:
            decision = EmailDecision.NOT_ACTIONABLE

        self.logger.info(
            "[ClassificationAgent] type=%s actionable=%s",
            email_type,
            is_actionable,
        )

        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            data={
                "email_type": email_type,
                "is_actionable": is_actionable,
                "early_decision": decision,
                "envelope": envelope,
            },
        )
