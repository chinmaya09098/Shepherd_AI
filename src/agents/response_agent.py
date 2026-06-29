"""
Response Agent.

Generates and sends email responses within the same Outlook thread using
Graph API. Three response types:

  acknowledgment — immediate "we received your request" reply
  follow_up      — requests missing fields from the customer
  confirmation   — confirms shipment was created (auto-approve path)

Uses Jinja2 HTML templates from src/templates/ for the email bodies.
Falls back to plain-text if templates are unavailable.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.agents.base_agent import BaseAgent
from src.models.agent_models import AgentResult, AgentStatus, AgentTask, EmailDecision
from src.models.shipment import Shipment
from src.utils.logger import get_logger

logger = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# Human-readable labels for missing field keys
_FIELD_LABELS: Dict[str, str] = {
    "customerName":   "Customer Name",
    "pickupLocation": "Pickup Location (street, city, state, zip)",
    "dropLocation":   "Delivery Location (street, city, state, zip)",
    "shipperZip":     "Pickup Zip Code",
    "consigneeZip":   "Delivery Zip Code",
    "pickupDate":     "Pickup Date",
    "deliveryDate":   "Delivery Date",
    "equipmentMode":  "Equipment / Mode",
    "items":          "Commodity Details (description, weight, pieces)",
}


class ResponseAgent(BaseAgent):
    """Generates and sends reply emails within the conversation thread."""

    def __init__(self, graph_client=None):
        super().__init__("ResponseAgent")
        self._graph_client = graph_client   # Optional GraphClient; None → dry-run

    def _execute(self, task: AgentTask) -> AgentResult:
        decision: EmailDecision = task.metadata.get("decision", EmailDecision.NOT_ACTIONABLE)
        shipments: List[Shipment] = task.metadata.get("shipments", [])
        missing: List[str] = task.metadata.get("blocking_missing", [])
        access_token: Optional[str] = task.metadata.get("access_token")
        message_id: str = task.message_id
        email_data: Dict[str, Any] = task.email_data

        sender = email_data.get("from", "")
        subject = email_data.get("subject", "")

        if decision == EmailDecision.AUTO_APPROVE:
            body = self._render_confirmation(shipments, subject)
            response_type = "confirmation"
        elif decision == EmailDecision.FOLLOW_UP:
            body = self._render_follow_up(missing, subject)
            response_type = "follow_up"
        elif decision == EmailDecision.HUMAN_REVIEW:
            body = self._render_acknowledgment(subject)
            response_type = "acknowledgment"
        elif decision == EmailDecision.SPAM:
            # Never reply to spam
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.SKIPPED,
                data={"response_type": "none", "sent": False},
            )
        else:
            # NOT_ACTIONABLE — optional acknowledgment
            body = self._render_acknowledgment(subject)
            response_type = "acknowledgment"

        sent = False
        if self._graph_client and access_token and message_id:
            try:
                import asyncio
                sent = asyncio.run(
                    self._graph_client.send_reply(
                        access_token=access_token,
                        message_id=message_id,
                        reply_body=body,
                        reply_html=True,
                    )
                )
            except Exception as exc:
                self.logger.error("send_reply failed: %s", exc)
        else:
            self.logger.info(
                "[ResponseAgent] DRY RUN — would send %s to %s",
                response_type,
                sender,
            )
            sent = True   # count as sent in dry-run mode

        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS if sent else AgentStatus.FAILED,
            data={
                "response_type": response_type,
                "sent": sent,
                "body_preview": body[:200],
            },
        )

    # ── Template rendering ─────────────────────────────────────────────────────

    def _render_follow_up(self, missing_fields: List[str], subject: str) -> str:
        tmpl_path = _TEMPLATES_DIR / "follow_up.html"
        if tmpl_path.exists():
            return _render_jinja(
                tmpl_path,
                subject=subject,
                missing_items=[
                    _FIELD_LABELS.get(f, f) for f in missing_fields
                ],
            )
        # Plain text fallback
        items_txt = "\n".join(f"  • {_FIELD_LABELS.get(f, f)}" for f in missing_fields)
        return (
            f"<p>Thank you for your shipment request regarding <b>{subject}</b>.</p>"
            f"<p>To proceed, we need the following information:</p>"
            f"<ul>{''.join(f'<li>{_FIELD_LABELS.get(f,f)}</li>' for f in missing_fields)}</ul>"
            f"<p>Please reply to this email with the missing details.</p>"
            f"<p>Thank you,<br>Shepherd AI — 3PL Systems</p>"
        )

    def _render_confirmation(self, shipments: List[Shipment], subject: str) -> str:
        tmpl_path = _TEMPLATES_DIR / "confirmation.html"
        shipment = shipments[0] if shipments else None
        if tmpl_path.exists() and shipment:
            rf = shipment.required_fields
            return _render_jinja(
                tmpl_path,
                subject=subject,
                customer_name=rf.customer_name or "",
                pickup=str(rf.pickup_location.address if rf.pickup_location else ""),
                delivery=str(rf.drop_location.address if rf.drop_location else ""),
                pickup_date=str(rf.pickup_date or ""),
                delivery_date=str(rf.delivery_date or ""),
                equipment=rf.equipment_mode or "",
            )
        return (
            f"<p>Thank you for your shipment request regarding <b>{subject}</b>.</p>"
            f"<p>Your shipment has been <b>confirmed and booked</b> in our system.</p>"
            f"<p>Our team will be in touch with further details.</p>"
            f"<p>Thank you,<br>Shepherd AI — 3PL Systems</p>"
        )

    def _render_acknowledgment(self, subject: str) -> str:
        tmpl_path = _TEMPLATES_DIR / "acknowledgment.html"
        if tmpl_path.exists():
            return _render_jinja(tmpl_path, subject=subject)
        return (
            f"<p>Thank you for your message regarding <b>{subject}</b>.</p>"
            f"<p>We have received your request and our team will review it shortly.</p>"
            f"<p>Thank you,<br>Shepherd AI — 3PL Systems</p>"
        )


def _render_jinja(template_path: Path, **context) -> str:
    """Render a Jinja2 HTML template with the given context variables."""
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            autoescape=select_autoescape(["html"]),
        )
        tmpl = env.get_template(template_path.name)
        return tmpl.render(**context)
    except ImportError:
        # Jinja2 not installed — read raw template
        with open(template_path, encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        logger.warning("Template render failed (%s): %s", template_path.name, exc)
        return ""
