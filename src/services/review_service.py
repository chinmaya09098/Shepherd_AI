"""
Human review queue service backed by Azure Table Storage.

Shipments that fail automated processing are queued here for human review.
Provides list and resolve operations for a review UI (Streamlit or separate tool).

Azure Table Storage:
  Table:         shepherdreviewqueue
  PartitionKey:  tenant_id
  RowKey:        review_id  (uuid4)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from src.models.review_models import ReviewItem, ReviewPriority, ReviewStatus
from src.utils.logger import get_logger

logger = get_logger(__name__)

_TABLE_NAME = "shepherdreviewqueue"


class ReviewService:
    """CRUD wrapper around the shepherd review-queue Azure Table."""

    def __init__(self, connection_string: str):
        self._table = None
        try:
            from azure.data.tables import TableServiceClient
            svc = TableServiceClient.from_connection_string(connection_string)
            self._table = svc.create_table_if_not_exists(_TABLE_NAME)
            logger.info("ReviewService ready (table: %s)", _TABLE_NAME)
        except ImportError:
            logger.warning("azure-data-tables not installed — ReviewService disabled")
        except Exception as exc:
            logger.error("ReviewService init failed: %s", exc)

    # ── Write ──────────────────────────────────────────────────────────────────

    def queue(self, item: ReviewItem) -> bool:
        """Add or update a review item in the queue."""
        if not self._table:
            return False
        try:
            entity = _item_to_entity(item)
            self._table.upsert_entity(entity)
            logger.info(
                "Review item queued: review_id=%s reason=%s",
                item.review_id,
                item.reason,
            )
            return True
        except Exception as exc:
            logger.error("ReviewService.queue failed: %s", exc)
            return False

    def resolve(
        self,
        review_id: str,
        status: ReviewStatus,
        reviewer_notes: Optional[str] = None,
        resolution_data: Optional[dict] = None,
        tenant_id: str = "default",
    ) -> bool:
        """Mark a review item as resolved (approved/rejected/escalated)."""
        if not self._table:
            return False
        try:
            entity = self._table.get_entity(
                partition_key=tenant_id,
                row_key=review_id,
            )
            entity["status"] = status.value
            entity["resolved_at"] = datetime.now(timezone.utc).isoformat()
            entity["updated_at"] = datetime.now(timezone.utc).isoformat()
            if reviewer_notes:
                entity["reviewer_notes"] = reviewer_notes
            if resolution_data:
                entity["resolution_data"] = json.dumps(resolution_data, default=str)
            self._table.update_entity(entity)
            return True
        except Exception as exc:
            logger.error("ReviewService.resolve failed: %s", exc)
            return False

    # ── Read ───────────────────────────────────────────────────────────────────

    def get(self, review_id: str, tenant_id: str = "default") -> Optional[ReviewItem]:
        if not self._table:
            return None
        try:
            entity = self._table.get_entity(
                partition_key=tenant_id,
                row_key=review_id,
            )
            return _entity_to_item(entity)
        except Exception:
            return None

    def list_pending(self, tenant_id: str = "default") -> List[ReviewItem]:
        """Return all pending review items, oldest first."""
        if not self._table:
            return []
        try:
            flt = f"PartitionKey eq '{tenant_id}' and status eq 'pending'"
            items = [_entity_to_item(e) for e in self._table.query_entities(flt)]
            return sorted(items, key=lambda x: x.created_at)
        except Exception as exc:
            logger.error("ReviewService.list_pending failed: %s", exc)
            return []

    def list_by_status(
        self,
        status: ReviewStatus,
        tenant_id: str = "default",
    ) -> List[ReviewItem]:
        if not self._table:
            return []
        try:
            flt = f"PartitionKey eq '{tenant_id}' and status eq '{status.value}'"
            return [_entity_to_item(e) for e in self._table.query_entities(flt)]
        except Exception as exc:
            logger.error("ReviewService.list_by_status failed: %s", exc)
            return []


# ── Serialisation helpers ──────────────────────────────────────────────────────

def _item_to_entity(item: ReviewItem) -> dict:
    entity: dict = {
        "PartitionKey": item.tenant_id,
        "RowKey": item.review_id,
    }
    for k, v in item.model_dump().items():
        if isinstance(v, (dict, list)):
            entity[k] = json.dumps(v, default=str)
        elif isinstance(v, datetime):
            entity[k] = v.isoformat()
        elif v is not None:
            entity[k] = v
    return entity


def _entity_to_item(entity: dict) -> ReviewItem:
    data: dict = {
        "review_id": entity.get("RowKey", ""),
        "tenant_id": entity.get("PartitionKey", "default"),
    }
    for k, v in entity.items():
        if k in ("PartitionKey", "RowKey", "etag", "Timestamp"):
            continue
        if isinstance(v, str) and v and v[0] in ("{", "["):
            try:
                data[k] = json.loads(v)
                continue
            except Exception:
                pass
        data[k] = v
    return ReviewItem.model_validate(data)
