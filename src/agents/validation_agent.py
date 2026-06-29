"""
Validation Agent.

Validates extracted shipment data against required field definitions.
Populates missing_required_fields on each Shipment and also detects
zip-code gaps that the LLM sometimes misses (mirrors _validate_zip_fields
in streamlit_app.py).

Produces a combined list of all missing fields across all shipments.
"""
from __future__ import annotations

from typing import List

from src.agents.base_agent import BaseAgent
from src.models.agent_models import AgentResult, AgentStatus, AgentTask
from src.models.shipment import Shipment


# Fields where presence means a sub-field (zip) may still be absent
_ZIP_FIELD_MAP = {
    "shipperZip": ("pickup_location",),
    "consigneeZip": ("drop_location",),
}


class ValidationAgent(BaseAgent):
    """Validates extracted shipments and compiles missing-field lists."""

    def __init__(self):
        super().__init__("ValidationAgent")

    def _execute(self, task: AgentTask) -> AgentResult:
        shipments: List[Shipment] = task.metadata.get("shipments", [])

        if not shipments:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.SKIPPED,
                data={"all_missing": [], "valid_count": 0},
                error="No shipments to validate",
            )

        all_missing: List[str] = []

        for shipment in shipments:
            # Run the zip-code gap check
            _check_zip_fields(shipment)

            missing = shipment.missing_required_fields or []
            if missing:
                all_missing.extend(m for m in missing if m not in all_missing)
                self.logger.info(
                    "[ValidationAgent] shipment has missing fields: %s",
                    missing,
                )
            else:
                self.logger.info("[ValidationAgent] shipment is complete")

        valid_count = sum(
            1 for s in shipments if not s.missing_required_fields
        )

        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            data={
                "shipments": shipments,
                "all_missing": all_missing,
                "valid_count": valid_count,
                "total_count": len(shipments),
            },
        )


def _check_zip_fields(shipment: Shipment) -> None:
    """
    Post-extraction check for missing zip codes.
    The LLM flags missing fields at the location level but sometimes misses
    the zip specifically when the rest of the address is present.
    """
    rf = shipment.required_fields

    if (rf.pickup_location and
            not (rf.pickup_location.address and rf.pickup_location.address.zip_code)):
        if "shipperZip" not in shipment.missing_required_fields:
            shipment.missing_required_fields.append("shipperZip")

    if (rf.drop_location and
            not (rf.drop_location.address and rf.drop_location.address.zip_code)):
        if "consigneeZip" not in shipment.missing_required_fields:
            shipment.missing_required_fields.append("consigneeZip")
