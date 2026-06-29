"""
ShipMind / Brokerware API client (Week 7 integration — stub).

This module provides the interface for creating shipments in the ShipMind TMS.
The actual endpoint details, auth mechanism, and payload schema will be
confirmed during Week 7 of the SoW. Until then, the client logs intended
operations and returns a success response so the rest of the pipeline
continues to work.

To activate production integration:
  1. Set SHIPMIND_BASE_URL, SHIPMIND_API_KEY (or OAuth credentials) in .env
  2. Replace _stub_response() calls with real HTTP calls in create_shipment()
  3. Map Shepherd's Shipment model to ShipMind's expected schema
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import httpx

from src.models.shipment import Shipment
from src.utils.logger import get_logger

logger = get_logger(__name__)

_BASE_URL = os.getenv("SHIPMIND_BASE_URL", "")
_API_KEY = os.getenv("SHIPMIND_API_KEY", "")
_TIMEOUT = 30


class ShipMindClient:
    """
    Client for creating shipments in ShipMind / Brokerware.

    Production mode: enabled when SHIPMIND_BASE_URL is set.
    Stub mode: when SHIPMIND_BASE_URL is not set, operations are logged
               and a synthetic success response is returned for testing.
    """

    def __init__(self):
        self._base_url = os.getenv("SHIPMIND_BASE_URL", "").rstrip("/")
        self._api_key = os.getenv("SHIPMIND_API_KEY", "")
        self._is_stub = not bool(self._base_url)

        if self._is_stub:
            logger.warning(
                "ShipMindClient running in STUB mode — "
                "set SHIPMIND_BASE_URL and SHIPMIND_API_KEY to enable production calls"
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    async def create_shipment(
        self,
        shipment: Shipment,
        customer_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Submit a shipment to ShipMind for booking.

        Args:
            shipment:    Fully validated Shepherd Shipment model
            customer_id: Resolved Hyperion customer ID (nullable)

        Returns:
            Response dict with:
              {
                "success": bool,
                "shipment_id": str | None,    # ShipMind reference ID
                "message": str,
                "raw_response": dict | None,
              }
        """
        payload = self._build_payload(shipment, customer_id)

        if self._is_stub:
            return self._stub_response(payload)

        return await self._post("/shipments", payload)

    async def get_shipment_status(self, shipment_id: str) -> Dict[str, Any]:
        """Poll the status of a previously submitted shipment."""
        if self._is_stub:
            return {"success": True, "status": "pending", "shipment_id": shipment_id}
        return await self._get(f"/shipments/{shipment_id}")

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _build_payload(
        self,
        shipment: Shipment,
        customer_id: Optional[int],
    ) -> Dict[str, Any]:
        """
        Map Shepherd's Shipment model to ShipMind's expected JSON schema.
        TODO: update field mapping once ShipMind API spec is confirmed in Week 7.
        """
        rf = shipment.required_fields
        nth = shipment.nice_to_have_fields

        def _addr(loc) -> Dict[str, Any]:
            if not loc:
                return {}
            addr = loc.address or {}
            return {
                "name": loc.name or "",
                "street": getattr(addr, "street", "") or "",
                "city": getattr(addr, "city", "") or "",
                "state": getattr(addr, "state", "") or "",
                "zip": getattr(addr, "zip_code", "") or "",
                "country": getattr(addr, "country", "US") or "US",
            }

        items = []
        for item in (rf.items or []):
            items.append({
                "description": item.description or "",
                "quantity": item.quantity or item.pieces or 1,
                "weight": item.weight or 0,
                "unit": item.unit or "LBS",
                "pallets": item.pallets or 0,
            })

        return {
            "customerId": customer_id,
            "customerName": rf.customer_name or "",
            "equipmentMode": rf.equipment_mode or "",
            "pickupDate": rf.pickup_date.isoformat() if rf.pickup_date else "",
            "deliveryDate": rf.delivery_date.isoformat() if rf.delivery_date else "",
            "origin": _addr(rf.pickup_location),
            "destination": _addr(rf.drop_location),
            "items": items,
            "totalWeight": rf.total_weight or 0,
            "accessorials": rf.accessorials or [],
            "specialInstructions": nth.special_instructions or "",
            "temperature": nth.temperature or "",
            "referenceNumbers": nth.reference_numbers or {},
        }

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
                resp = await http.post(
                    f"{self._base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    content=json.dumps(payload),
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "success": True,
                    "shipment_id": data.get("id") or data.get("shipmentId"),
                    "message": "Shipment created successfully",
                    "raw_response": data,
                }
            return {
                "success": False,
                "shipment_id": None,
                "message": f"ShipMind returned {resp.status_code}: {resp.text[:200]}",
                "raw_response": None,
            }
        except Exception as exc:
            logger.error("ShipMindClient._post failed: %s", exc)
            return {
                "success": False,
                "shipment_id": None,
                "message": str(exc),
                "raw_response": None,
            }

    async def _get(self, path: str) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
                resp = await http.get(
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            if resp.status_code == 200:
                return {"success": True, "raw_response": resp.json()}
            return {"success": False, "message": f"{resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            logger.error("ShipMindClient._get failed: %s", exc)
            return {"success": False, "message": str(exc)}

    @staticmethod
    def _stub_response(payload: Dict[str, Any]) -> Dict[str, Any]:
        import uuid
        stub_id = f"STUB-{str(uuid.uuid4())[:8].upper()}"
        logger.info(
            "ShipMindClient STUB — would create shipment for customer='%s' mode='%s' stub_id=%s",
            payload.get("customerName", ""),
            payload.get("equipmentMode", ""),
            stub_id,
        )
        return {
            "success": True,
            "shipment_id": stub_id,
            "message": "Stub: shipment creation simulated (SHIPMIND_BASE_URL not set)",
            "raw_response": {"id": stub_id, "status": "created"},
        }
