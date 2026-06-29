"""
Shipment Creation Agent.

Submits validated shipments to the TMS (currently ShipMind/Brokerware stub,
activated fully in Week 7). Also saves shipment JSON to Azure Blob Storage
as the primary output artifact.

On success, adds 'tms_shipment_ids' to agent data so the ResponseAgent can
include booking references in the confirmation email.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.agents.base_agent import BaseAgent
from src.models.agent_models import AgentResult, AgentStatus, AgentTask
from src.models.shipment import Shipment
from src.models.client_format import format_client_json_str
from src.services.shipmind_client import ShipMindClient
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ShipmentCreationAgent(BaseAgent):
    """Submits approved shipments to ShipMind TMS and saves output JSON."""

    def __init__(self):
        super().__init__("ShipmentCreationAgent")
        self._shipmind = ShipMindClient()

    def _execute(self, task: AgentTask) -> AgentResult:
        shipments: List[Shipment] = task.metadata.get("shipments", [])
        customer_id: Optional[int] = task.metadata.get("customer_id")

        if not shipments:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.SKIPPED,
                error="No shipments to create",
            )

        results: List[Dict[str, Any]] = []
        tms_ids: List[str] = []
        all_success = True

        for i, shipment in enumerate(shipments):
            # Skip spam or non-tender
            if shipment.email_type == "spam":
                continue

            # Save output JSON to Azure Blob Storage
            blob_saved = self._save_to_blob(
                shipment=shipment,
                email_name=task.email_data.get("subject", task.message_id),
                source_name=f"shipment_{i+1}",
                index=i + 1,
                customer_id=customer_id,
            )

            # Submit to TMS (async in a sync context)
            try:
                import asyncio
                tms_result = asyncio.run(
                    self._shipmind.create_shipment(shipment, customer_id)
                )
            except Exception as exc:
                tms_result = {"success": False, "message": str(exc)}

            if tms_result.get("success"):
                sid = tms_result.get("shipment_id", "")
                tms_ids.append(sid)
                self.logger.info(
                    "Shipment created in TMS: tms_id=%s customer_id=%s",
                    sid,
                    customer_id,
                )
            else:
                all_success = False
                self.logger.error(
                    "TMS creation failed: %s",
                    tms_result.get("message", "unknown error"),
                )

            results.append({
                "shipment_index": i + 1,
                "blob_saved": blob_saved,
                "tms_result": tms_result,
            })

        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS if all_success else AgentStatus.FAILED,
            data={
                "creation_results": results,
                "tms_shipment_ids": tms_ids,
                "customer_id": customer_id,
            },
        )

    def _save_to_blob(
        self,
        shipment: Shipment,
        email_name: str,
        source_name: str,
        index: int,
        customer_id: Optional[int],
    ) -> bool:
        """Save shipment JSON to Azure Blob Storage output container."""
        try:
            from src.config import Config
            from azure.storage.blob import BlobServiceClient
            from pathlib import Path

            if not Config.AZURE_STORAGE_CONNECTION_STRING or not Config.AZURE_STORAGE_OUTPUT_CONTAINER:
                return False

            bsc = BlobServiceClient.from_connection_string(Config.AZURE_STORAGE_CONNECTION_STRING)
            container = bsc.get_container_client(Config.AZURE_STORAGE_OUTPUT_CONTAINER)

            json_content = format_client_json_str(shipment, customer_id=customer_id)
            email_folder = Path(email_name).stem.replace(" ", "_")[:50]
            src_part = Path(source_name).stem.replace(" ", "_")[:50]
            blob_name = f"output-json/{email_folder}/shipment_{index}_{src_part}.json"

            blob = container.get_blob_client(blob_name)
            blob.upload_blob(json_content, overwrite=True)
            self.logger.info("Saved to blob: %s", blob_name)
            return True
        except Exception as exc:
            self.logger.error("Blob save failed: %s", exc)
            return False
