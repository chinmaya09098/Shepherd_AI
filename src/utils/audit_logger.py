"""
Audit logger for Shepherd AI compliance trail.

Writes structured audit records to Azure Table Storage
(table: shepherdauditlog) and optionally to Azure Blob Storage.

Every processed email produces at least one audit record capturing:
  - who triggered the action (system / tenant)
  - what happened (decision, response sent, TMS IDs)
  - when it happened (ISO-8601 UTC)
  - correlation / message IDs for cross-service tracing

Usage:
    from src.utils.audit_logger import log_pipeline_result, log_event

    log_pipeline_result(result, tenant_id="default")
    log_event("subscription_renewed", tenant_id="default", details={"sub_id": "..."})

Audit logging is a no-op when AZURE_STORAGE_CONNECTION_STRING is not set,
so local development works without any Azure credentials.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("shepherd.audit")

_TABLE_NAME = "shepherdauditlog"
_BLOB_CONTAINER = os.getenv("AZURE_STORAGE_LOGS_CONTAINER", "processed-logs")

# Module-level singleton table client
_table_client = None
_initialized = False


def _get_table_client():
    global _table_client, _initialized
    if _initialized:
        return _table_client

    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        _initialized = True
        return None

    try:
        from azure.data.tables import TableServiceClient

        svc = TableServiceClient.from_connection_string(conn_str)
        svc.create_table_if_not_exists(_TABLE_NAME)
        _table_client = svc.get_table_client(_TABLE_NAME)
        logger.info("AuditLogger connected to table '%s'", _TABLE_NAME)
    except ImportError:
        logger.debug("azure-data-tables not installed — audit logging disabled")
    except Exception as exc:
        logger.warning("AuditLogger init failed: %s", exc)

    _initialized = True
    return _table_client


# ── Public API ──────────────────────────────────────────────────────────────


def log_event(
    action: str,
    tenant_id: str = "default",
    message_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    outcome: str = "success",
) -> None:
    """
    Write a single audit record to Azure Table Storage.

    Args:
        action:          Verb describing what happened, e.g. 'email_processed',
                         'subscription_renewed', 'review_queued'
        tenant_id:       Tenant identifier (used as PartitionKey)
        message_id:      Graph message ID (if applicable)
        conversation_id: Outlook conversation/thread ID (if applicable)
        correlation_id:  Orchestrator correlation ID for tracing
        details:         Arbitrary key-value pairs to include in the record
        outcome:         'success', 'failure', 'skipped', etc.
    """
    client = _get_table_client()
    if client is None:
        return

    record_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()

    entity: Dict[str, Any] = {
        # Azure Table keys
        "PartitionKey": tenant_id,
        "RowKey": record_id,
        # Standard audit fields
        "timestamp": ts,
        "action": action,
        "outcome": outcome,
        "message_id": message_id or "",
        "conversation_id": conversation_id or "",
        "correlation_id": correlation_id or "",
        # Variable payload serialised as JSON string
        "details": json.dumps(details or {}, default=str),
    }

    try:
        client.upsert_entity(entity)
    except Exception as exc:
        # Audit failures must NEVER crash the pipeline
        logger.warning("AuditLogger.log_event failed (action=%s): %s", action, exc)


def log_pipeline_result(result, tenant_id: str = "default") -> None:
    """
    Convenience function that writes an audit record from a PipelineResult.

    Args:
        result:    PipelineResult returned by Orchestrator.process()
        tenant_id: Tenant identifier
    """
    if result is None:
        return

    # Build a detail dict without circular references
    details: Dict[str, Any] = {
        "email_type": result.email_type or "",
        "decision": result.decision.value if result.decision else "",
        "response_sent": result.response_sent,
        "missing_fields": result.missing_fields or [],
        "follow_up_fields": result.follow_up_fields or [],
        "review_reason": getattr(result, "review_reason", ""),
        "error": result.error or "",
        "shipment_count": len(result.shipments) if result.shipments else 0,
        "agent_trace": result.agent_results or [],
    }

    if result.processing_start and result.processing_end:
        duration_ms = (
            result.processing_end - result.processing_start
        ).total_seconds() * 1000
        details["duration_ms"] = round(duration_ms, 1)

    outcome = "failure" if result.error else "success"
    if result.error == "duplicate":
        outcome = "skipped"

    log_event(
        action="email_processed",
        tenant_id=tenant_id,
        message_id=result.message_id,
        conversation_id=result.conversation_id,
        correlation_id=result.correlation_id,
        details=details,
        outcome=outcome,
    )


def log_webhook_event(
    event_type: str,
    subscription_id: str,
    tenant_id: str = "default",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Log a webhook lifecycle event (subscription created, renewed, deleted, etc.).

    Args:
        event_type:      e.g. 'subscription_created', 'subscription_renewed'
        subscription_id: Graph subscription ID
        tenant_id:       Tenant identifier
        details:         Additional context
    """
    log_event(
        action=event_type,
        tenant_id=tenant_id,
        details={**(details or {}), "subscription_id": subscription_id},
    )


def log_review_event(
    event_type: str,
    review_id: str,
    tenant_id: str = "default",
    message_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Log a human-review queue event (queued, resolved, escalated).

    Args:
        event_type: e.g. 'review_queued', 'review_resolved'
        review_id:  UUID of the ReviewItem
        tenant_id:  Tenant identifier
        message_id: Associated Graph message ID
        details:    Additional context
    """
    log_event(
        action=event_type,
        tenant_id=tenant_id,
        message_id=message_id,
        details={**(details or {}), "review_id": review_id},
    )
