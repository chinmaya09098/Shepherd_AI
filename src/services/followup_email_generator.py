"""
Follow-up email generator for missing shipment fields.

Uses Azure OpenAI to produce a professional, context-aware HTML email body
that politely requests only the specific fields still missing from a customer's
shipment request.  Falls back to a static template if the LLM call fails.
"""
from __future__ import annotations

from typing import List, Optional

from openai import AzureOpenAI

from src.config import Config
from src.models.shipment import Shipment
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Human-readable labels for each field key that may appear in missing_required_fields
# ---------------------------------------------------------------------------
FIELD_HUMAN_LABELS: dict[str, str] = {
    "pickupLocation": "Pickup / origin location (company name, street address, city, state, zip code)",
    "dropLocation":   "Delivery / destination location (company name, street address, city, state, zip code)",
    "pickupDate":     "Pickup date — when the freight will be ready for collection",
    "deliveryDate":   "Required delivery / arrival date",
    "equipmentMode":  "Equipment or trailer type (e.g. Dry Van, Flatbed, Reefer, LTL, Step Deck)",
    "items":          "Freight / commodity details (description, total weight, piece count, dimensions)",
    "totalWeight":    "Total shipment weight in lbs",
    "customerName":   "Account or customer name for this shipment",
    "shipperZip":     "Pickup location zip / postal code",
    "consigneeZip":   "Delivery location zip / postal code",
}


class FollowupEmailGenerator:
    """
    Generates professional follow-up emails requesting missing shipment information.

    Each follow-up is tailored to:
    - the specific fields still missing (no generic "please provide all details")
    - the shipment context already captured (so the customer knows we received their request)
    - the customer's name if available (personalised greeting)
    """

    def __init__(self) -> None:
        if not all([Config.AZURE_OPENAI_ENDPOINT, Config.AZURE_OPENAI_KEY, Config.AZURE_OPENAI_DEPLOYMENT]):
            raise ValueError("Azure OpenAI must be configured to use FollowupEmailGenerator")

        self._client = AzureOpenAI(
            api_key=Config.AZURE_OPENAI_KEY,
            api_version=Config.AZURE_OPENAI_API_VERSION,
            azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
        )
        self._deployment = Config.AZURE_OPENAI_DEPLOYMENT

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_followup_email(
        self,
        missing_fields: List[str],
        shipment: Shipment,
        original_subject: str,
        sender_name: Optional[str] = None,
        followup_number: int = 1,
    ) -> str:
        """
        Generate an HTML email body requesting the missing shipment fields.

        Args:
            missing_fields:   List of field keys that are still missing
                              (subset of RequiredFields attribute names).
            shipment:         The partially extracted Shipment object — used to
                              build a "what we already know" summary so the customer
                              sees we parsed their original email.
            original_subject: Subject line of the customer's original email.
            sender_name:      Customer's first name for personalised greeting (optional).
            followup_number:  Which follow-up this is (1 = first, 2 = second, …).

        Returns:
            HTML string suitable for passing directly to GraphClient.send_reply().
        """
        missing_labels = [
            FIELD_HUMAN_LABELS.get(f, f.replace("_", " ").title())
            for f in missing_fields
        ]

        known_lines = self._build_known_summary(shipment)
        reference_line = self._build_reference_line(shipment, original_subject)
        greeting = f"Hi {sender_name}," if sender_name else "Hi,"

        ordinal = {1: "first", 2: "second", 3: "third"}.get(followup_number, f"#{followup_number}")
        followup_note = (
            "" if followup_number == 1
            else f"<p>This is our {ordinal} request for the information below — "
                 f"please reply at your earliest convenience so we can process your shipment.</p>"
        )

        prompt = f"""You are a professional freight brokerage coordinator at 3PL Systems.
Write a polite, concise HTML follow-up email body to a customer requesting missing shipment information.

Context:
- Greeting: "{greeting}"
- Reference: {reference_line}
- This is follow-up number {followup_number}
{f"- Already known: {'; '.join(known_lines)}" if known_lines else ""}

Missing information needed (ONLY ask for these — do not ask for anything else):
{chr(10).join(f'{i + 1}. {label}' for i, label in enumerate(missing_labels))}

Instructions:
- Open with the greeting above
- In 1-2 sentences acknowledge the shipment request using the reference line
- If we already know some details, briefly confirm them so the customer knows their request was received
- List the missing items using a clear numbered HTML list (<ol>)
- Keep the tone professional, helpful, and concise — not demanding
- Close by asking them to reply to this same email thread with the missing details
- Sign off as: "Shepherd AI Shipment Team / 3PL Systems"
- Return ONLY the inner HTML body content (no <html>, <head>, or <body> tags)
- Use simple inline styles for any formatting

Return only valid HTML body content."""

        try:
            response = self._client.chat.completions.create(
                model=self._deployment,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a professional freight logistics coordinator. "
                            "Write polished, concise HTML email bodies. Return HTML only, no markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            content = (response.choices[0].message.content or "").strip()
            # Strip markdown code fences if the model wrapped the response
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
            logger.info(
                "Generated follow-up email #%d for %d missing fields",
                followup_number,
                len(missing_fields),
            )
            return content

        except Exception as exc:
            logger.error("OpenAI follow-up email generation failed: %s", exc)
            return self._fallback_email(greeting, reference_line, missing_labels, followup_note)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_known_summary(self, shipment: Shipment) -> List[str]:
        """Build a short list of what was already extracted, for the 'known info' context."""
        lines: List[str] = []
        rf = shipment.required_fields

        if rf.pickup_location:
            loc = rf.pickup_location
            parts = [loc.name or ""]
            if loc.address:
                if loc.address.city:
                    parts.append(f"{loc.address.city}, {loc.address.state or ''}".strip(", "))
            line = ", ".join(p for p in parts if p)
            if line:
                lines.append(f"Pickup: {line}")

        if rf.drop_location:
            loc = rf.drop_location
            parts = [loc.name or ""]
            if loc.address:
                if loc.address.city:
                    parts.append(f"{loc.address.city}, {loc.address.state or ''}".strip(", "))
            line = ", ".join(p for p in parts if p)
            if line:
                lines.append(f"Delivery: {line}")

        if rf.pickup_date:
            lines.append(f"Pickup date: {rf.pickup_date.strftime('%B %d, %Y')}")

        if rf.equipment_mode:
            lines.append(f"Equipment: {rf.equipment_mode}")

        if rf.items:
            lines.append(f"{len(rf.items)} commodity line(s) captured")

        return lines

    def _build_reference_line(self, shipment: Shipment, original_subject: str) -> str:
        """Build a short reference string for the email (order#, PO#, or subject)."""
        ntf = shipment.nice_to_have_fields
        parts: List[str] = []
        if ntf.order_number:
            parts.append(f"Order #{ntf.order_number}")
        if ntf.purchase_order:
            parts.append(f"PO #{ntf.purchase_order}")
        if ntf.shipment_id:
            parts.append(f"Shipment #{ntf.shipment_id}")
        return ", ".join(parts) if parts else f'"{original_subject}"'

    @staticmethod
    def _fallback_email(
        greeting: str,
        reference_line: str,
        missing_labels: List[str],
        followup_note: str,
    ) -> str:
        """Static HTML fallback used when the OpenAI call fails."""
        items_html = "".join(f"<li>{label}</li>" for label in missing_labels)
        return f"""
<p>{greeting}</p>
<p>Thank you for your shipment request regarding <strong>{reference_line}</strong>.
We have received your email and are working to process your shipment.</p>
{followup_note}
<p>To proceed, we need the following additional information:</p>
<ol style="line-height:1.8;">{items_html}</ol>
<p>Please reply to this email with the details above and we will process your shipment as soon as possible.</p>
<p>Best regards,<br>
<strong>Shepherd AI Shipment Team</strong><br>
3PL Systems</p>
""".strip()
