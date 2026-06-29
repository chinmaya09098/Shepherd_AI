"""
Extraction Agent.

Runs the full 3-pass extraction pipeline on a classified shipment_tender email:
  Pass 0 — OCR all attachments via Azure Content Understanding
  Pass 1 — Extract envelope (email context + classification hints)
  Pass 2 — Extract per-attachment shipment data
  Pass 3 — (Optional) Extract per-source structured fields for fallback

Returns a list of extracted Shipment objects in agent data['shipments'].
Also carries forward the envelope and per-source breakdown for downstream agents.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List

from src.agents.base_agent import BaseAgent
from src.extractors.content_understanding import ContentUnderstandingExtractor
from src.extractors.openai_agent import OpenAIAgent
from src.models.agent_models import AgentResult, AgentStatus, AgentTask
from src.models.shipment import Shipment, ShipmentItem
from src.services.search_client import find_customer_matches


class ExtractionAgent(BaseAgent):
    """Extracts shipment data from attachments and/or email body."""

    def __init__(self):
        super().__init__("ExtractionAgent")
        self._content_extractor = ContentUnderstandingExtractor()
        self._openai = OpenAIAgent()

    def _execute(self, task: AgentTask) -> AgentResult:
        email_data = task.email_data
        envelope = task.metadata.get("envelope") or {}

        attachments = email_data.get("attachments", [])
        body = email_data.get("body", "")

        # ── Customer resolution ───────────────────────────────────────────────
        customer_id = self._resolve_customer(
            email_data.get("from", ""),
            email_data.get("to", ""),
            email_data.get("subject", ""),
        )

        # ── OCR pass ─────────────────────────────────────────────────────────
        attachment_data = []
        raw_cu: Dict[str, Any] = {}
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".ico"}

        processable = [
            a for a in attachments
            if Path(a.get("filename", "")).suffix.lower() not in image_exts
        ]

        for att in processable:
            filepath = att.get("filepath", "")
            if not filepath:
                continue
            result = self._content_extractor.extract_text(filepath)
            if result:
                text, confidence, raw = result
                attachment_data.append(
                    {"attachment": att, "text": text, "confidence": confidence}
                )
                if raw:
                    raw_cu[att["filename"]] = raw
            else:
                self.logger.warning("No text extracted from %s", att.get("filename"))

        # ── Envelope (re-extract with full attachment text if not already done) ──
        if not envelope:
            all_att_texts = "\n\n---\n\n".join(
                f"[{d['attachment']['filename']}]\n{d['text']}"
                for d in attachment_data
            )
            try:
                envelope = self._openai.extract_email_envelope(
                    email_body=body,
                    all_attachment_texts=all_att_texts,
                )
            except Exception as exc:
                self.logger.error("Envelope extraction failed: %s", exc)
                envelope = {}

        # ── Per-source structured breakdown ───────────────────────────────────
        source_breakdown: Dict[str, Any] = {}
        try:
            source_breakdown["emailBody"] = self._openai.extract_source_fields(body)
            for d in attachment_data:
                fname = d["attachment"]["filename"]
                source_breakdown[fname] = self._openai.extract_source_fields(d["text"])
        except Exception as exc:
            self.logger.warning("Source breakdown extraction failed: %s", exc)

        # ── Body shipments (unrelated to attachments) ─────────────────────────
        shipments: List[Shipment] = []
        if not envelope.get("bodyRelatedToAttachments", True):
            try:
                body_ships = self._openai.extract_body_shipments(body, envelope)
                shipments.extend(body_ships)
                self.logger.info(
                    "Extracted %d body shipments", len(body_ships)
                )
            except Exception as exc:
                self.logger.error("Body extraction failed: %s", exc)

        # ── Pass 2: per-attachment extraction ─────────────────────────────────
        if attachment_data:
            all_att_texts = "\n\n---\n\n".join(
                f"[{d['attachment']['filename']}]\n{d['text']}"
                for d in attachment_data
            )
            # Re-run envelope with full text for accuracy
            try:
                envelope = self._openai.extract_email_envelope(
                    email_body=body,
                    all_attachment_texts=all_att_texts,
                )
            except Exception:
                pass

            for d in attachment_data:
                fname = d["attachment"]["filename"]
                try:
                    shipment = self._openai.extract_shipment_data(
                        attachment_text=d["text"],
                        envelope=envelope,
                    )
                    if shipment:
                        # Apply source breakdown fallback for items
                        self._apply_items_fallback(shipment, fname, source_breakdown)
                        shipment.nice_to_have_fields.extraction_confidence = d["confidence"]
                        shipments.append(shipment)
                        self.logger.info(
                            "Extracted shipment from %s confidence=%.2f",
                            fname,
                            d["confidence"],
                        )
                except Exception as exc:
                    self.logger.error("Extraction failed for %s: %s", fname, exc)

        # ── No attachments: body-only extraction ─────────────────────────────
        if not attachment_data and not shipments:
            try:
                shipment = self._openai.extract_shipment_data(
                    attachment_text=body,
                    envelope=None,
                )
                if shipment:
                    shipments.append(shipment)
            except Exception as exc:
                self.logger.error("Body-only extraction failed: %s", exc)

        if not shipments:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                error="No shipments could be extracted",
                data={"customer_id": customer_id, "envelope": envelope},
            )

        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            data={
                "shipments": shipments,
                "customer_id": customer_id,
                "envelope": envelope,
                "source_breakdown": source_breakdown,
                "raw_cu_results": raw_cu,
            },
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_customer(
        self,
        sender: str,
        receiver: str,
        subject: str,
    ) -> int | None:
        try:
            result = find_customer_matches(sender, receiver)
            matches = result.get("matches", [])
            is_broker = result.get("is_broker_match", False)
            if is_broker and matches:
                return matches[0]["customerId"]
            if matches:
                return self._openai.resolve_customer_id(
                    sender_email=sender,
                    email_subject=subject,
                    customer_list=matches,
                )
        except Exception as exc:
            self.logger.warning("Customer resolution failed: %s", exc)
        return None

    @staticmethod
    def _apply_items_fallback(
        shipment: Shipment,
        filename: str,
        source_breakdown: Dict[str, Any],
    ) -> None:
        """Use source breakdown items when they are more granular than Pass 2."""
        raw_items = source_breakdown.get(filename, {}).get("items", [])
        if not raw_items or not isinstance(raw_items, list):
            return
        fallback = [
            ShipmentItem.model_validate({
                "description": i.get("description"),
                "pieces":      i.get("pieces") or i.get("quantity"),
                "weight":      i.get("weight"),
                "unit":        i.get("unit"),
                "pallets":     i.get("pallets"),
                "dimensions":  i.get("dimensions"),
                "quantity":    i.get("quantity"),
            })
            for i in raw_items if isinstance(i, dict)
        ]
        if len(fallback) > len(shipment.required_fields.items):
            shipment.required_fields.items = fallback
