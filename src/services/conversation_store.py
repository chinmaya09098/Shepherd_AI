"""
Conversation state store backed by Azure Table Storage.

Tracks multi-turn email conversations: when Shepherd sends a follow-up
requesting missing fields, the partial extraction state is stored here.
When the customer replies (same conversationId), the state is loaded,
new data is merged, and the pipeline continues.

Azure Table Storage:
  Table:         shepherdconversations
  PartitionKey:  tenant_id
  RowKey:        conversation_id  (Outlook conversationId)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.models.conversation_models import ConversationState, ConversationStatus
from src.utils.logger import get_logger

logger = get_logger(__name__)

_TABLE_NAME = "shepherdconversations"


class ConversationStore:
    """CRUD wrapper around the shepherd-conversations Azure Table."""

    def __init__(self, connection_string: str):
        self._table = None
        try:
            from azure.data.tables import TableServiceClient
            service = TableServiceClient.from_connection_string(connection_string)
            self._table = service.create_table_if_not_exists(_TABLE_NAME)
            logger.info("ConversationStore ready (table: %s)", _TABLE_NAME)
        except ImportError:
            logger.warning("azure-data-tables not installed — ConversationStore disabled")
        except Exception as exc:
            logger.error("ConversationStore init failed: %s", exc)

    # ── Write ──────────────────────────────────────────────────────────────────

    def upsert(self, state: ConversationState) -> bool:
        """Insert or replace a conversation state record."""
        if not self._table:
            return False
        try:
            entity = _state_to_entity(state)
            self._table.upsert_entity(entity)
            return True
        except Exception as exc:
            logger.error("ConversationStore.upsert failed: %s", exc)
            return False

    def delete(self, conversation_id: str, tenant_id: str = "default") -> bool:
        if not self._table:
            return False
        try:
            self._table.delete_entity(
                partition_key=tenant_id,
                row_key=conversation_id,
            )
            return True
        except Exception as exc:
            logger.error("ConversationStore.delete failed: %s", exc)
            return False

    # ── Read ───────────────────────────────────────────────────────────────────

    def get(
        self,
        conversation_id: str,
        tenant_id: str = "default",
    ) -> Optional[ConversationState]:
        """Retrieve a conversation by its Outlook conversationId."""
        if not self._table:
            return None
        try:
            entity = self._table.get_entity(
                partition_key=tenant_id,
                row_key=conversation_id,
            )
            return _entity_to_state(entity)
        except Exception:
            return None

    def list_active(self, tenant_id: str = "default") -> List[ConversationState]:
        """Return all ACTIVE conversations for a tenant."""
        if not self._table:
            return []
        try:
            flt = f"PartitionKey eq '{tenant_id}' and status eq 'active'"
            return [_entity_to_state(e) for e in self._table.query_entities(flt)]
        except Exception as exc:
            logger.error("ConversationStore.list_active failed: %s", exc)
            return []

    def list_expired(self, tenant_id: str = "default") -> List[ConversationState]:
        """Return conversations that have passed their expiry time."""
        now = datetime.now(timezone.utc).isoformat()
        if not self._table:
            return []
        try:
            flt = f"PartitionKey eq '{tenant_id}' and expires_at le '{now}' and status eq 'active'"
            return [_entity_to_state(e) for e in self._table.query_entities(flt)]
        except Exception as exc:
            logger.error("ConversationStore.list_expired failed: %s", exc)
            return []


# ── Serialisation helpers ──────────────────────────────────────────────────────

def _state_to_entity(state: ConversationState) -> dict:
    entity: dict = {
        "PartitionKey": state.tenant_id,
        "RowKey": state.conversation_id,
    }
    for k, v in state.model_dump().items():
        if isinstance(v, (dict, list)):
            entity[k] = json.dumps(v, default=str)
        elif isinstance(v, datetime):
            entity[k] = v.isoformat()
        elif v is not None:
            entity[k] = v
    return entity


def _entity_to_state(entity: dict) -> ConversationState:
    data: dict = {
        "conversation_id": entity.get("RowKey", ""),
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
    return ConversationState.model_validate(data)
