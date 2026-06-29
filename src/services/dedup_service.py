"""
Email deduplication service backed by Azure Table Storage.

Prevents the same Graph message_id from being processed more than once when:
  - A webhook fires twice for the same message
  - Polling overlaps with a webhook notification
  - A Function App retry triggers after a transient failure

Azure Table Storage:
  Table:         shepherddedup
  PartitionKey:  tenant_id
  RowKey:        sanitised message_id

Records are kept for DEFAULT_TTL_HOURS then may be pruned.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

_TABLE_NAME = "shepherddedup"
_DEFAULT_TTL_HOURS = 72          # Keep records for 3 days


class DedupService:
    """Tracks processed Graph message IDs to prevent double-processing."""

    def __init__(self, connection_string: str):
        self._table = None
        try:
            from azure.data.tables import TableServiceClient
            service = TableServiceClient.from_connection_string(connection_string)
            self._table = service.create_table_if_not_exists(_TABLE_NAME)
            logger.info("DedupService ready (table: %s)", _TABLE_NAME)
        except ImportError:
            logger.warning("azure-data-tables not installed — DedupService disabled")
        except Exception as exc:
            logger.error("DedupService init failed: %s", exc)

    def is_processed(self, message_id: str, tenant_id: str = "default") -> bool:
        """
        Return True if this message_id has already been recorded as processed.
        Returns False (allow processing) when the table is unavailable.
        """
        if not self._table:
            return False
        try:
            self._table.get_entity(
                partition_key=tenant_id,
                row_key=_safe_key(message_id),
            )
            return True
        except Exception:
            return False

    def mark_processed(
        self,
        message_id: str,
        tenant_id: str = "default",
        status: str = "processed",
    ) -> bool:
        """
        Record that a message has been processed.

        Args:
            message_id: Graph message ID
            tenant_id:  Tenant identifier
            status:     Processing outcome ('processed', 'failed', 'spam', 'skipped')
        """
        if not self._table:
            return False
        try:
            now = datetime.now(timezone.utc)
            entity = {
                "PartitionKey": tenant_id,
                "RowKey": _safe_key(message_id),
                "message_id": message_id,
                "status": status,
                "processed_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=_DEFAULT_TTL_HOURS)).isoformat(),
            }
            self._table.upsert_entity(entity)
            return True
        except Exception as exc:
            logger.error("DedupService.mark_processed failed: %s", exc)
            return False

    def get_status(
        self,
        message_id: str,
        tenant_id: str = "default",
    ) -> Optional[str]:
        """Return the recorded status for a message_id, or None if not found."""
        if not self._table:
            return None
        try:
            entity = self._table.get_entity(
                partition_key=tenant_id,
                row_key=_safe_key(message_id),
            )
            return entity.get("status")
        except Exception:
            return None


def _safe_key(message_id: str) -> str:
    """
    Azure Table Storage row keys cannot contain /\\#? and must be ≤1024 chars.
    Graph message IDs sometimes contain these characters.
    """
    safe = message_id.replace("/", "_").replace("\\", "_")
    safe = safe.replace("#", "_").replace("?", "_")
    return safe[:1024]
