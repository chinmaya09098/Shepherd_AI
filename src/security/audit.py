"""
Structured security audit logging to Application Insights.

SOW requirement: "Logging, monitoring, diagnostics, and operational
observability will be implemented using Azure Monitor, Application Insights,
Log Analytics, Azure Blob Storage, and Azure-native monitoring capabilities
to support issue detection, alerting, operational visibility, and full audit
traceability across the shipment orchestration lifecycle."

How it works
------------
Security events are emitted as Application Insights *custom events*
(``TelemetryClient.track_event``).  Each event has:
  - A name  (e.g. "apim_auth_rejection", "prompt_injection_detected")
  - A properties dict (searchable in Log Analytics / KQL)
  - A severity-mapped trace for alert rules

Log Analytics KQL to query security events:
    customEvents
    | where name startswith "shepherd_"
    | where timestamp > ago(24h)
    | order by timestamp desc

When Application Insights is NOT configured (APPLICATIONINSIGHTS_CONNECTION_STRING
not set), events fall through to the standard Python logger so local dev still
produces output.

Setup
-----
Set the env var (or Key Vault secret):
    APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...;IngestionEndpoint=...
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("shepherd_ai.security.audit")

# Module-level telemetry client (lazy-initialised once)
_telemetry_client = None
_client_initialised = False

# Well-known event name prefix keeps events filterable in Log Analytics
_PREFIX = "shepherd_"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def emit(event_name: str, properties: Optional[Dict[str, Any]] = None) -> None:
    """Emit a security audit event.

    Args:
        event_name:  Short snake_case name (e.g. "apim_auth_rejection").
                     The prefix ``shepherd_`` is added automatically.
        properties:  Flat dict of string/number properties for KQL filtering.
    """
    props = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        **(properties or {}),
    }

    # Sanitize: ensure all values are strings (App Insights requirement)
    safe_props = {k: str(v) for k, v in props.items()}
    full_name  = f"{_PREFIX}{event_name}"

    # 1. Application Insights
    client = _get_client()
    if client:
        try:
            client.track_event(full_name, properties=safe_props)
            client.flush()
        except Exception as exc:
            logger.debug("App Insights track_event failed: %s", exc)

    # 2. Structured log (always — captured by Azure Function log streaming)
    logger.info(
        "AUDIT event=%s %s",
        full_name,
        " ".join(f"{k}={v}" for k, v in safe_props.items()),
    )


def emit_email_processed(
    conversation_id: str,
    sender_email: str,
    mailbox: str,
    status: str,
    customer_id: Optional[int] = None,
) -> None:
    """Convenience wrapper for email pipeline audit events."""
    emit(
        "email_processed",
        {
            "conversation_id": conversation_id[:40],
            "sender_email":    sender_email,
            "mailbox":         mailbox,
            "status":          status,
            "customer_id":     str(customer_id) if customer_id else "",
        },
    )


def emit_followup_sent(
    conversation_id: str,
    followup_count: int,
    max_followups: int,
    customer_id: Optional[int] = None,
) -> None:
    """Audit event for each reminder follow-up email sent."""
    emit(
        "followup_sent",
        {
            "conversation_id": conversation_id[:40],
            "followup_count":  str(followup_count),
            "max_followups":   str(max_followups),
            "customer_id":     str(customer_id) if customer_id else "",
        },
    )


def emit_shipment_created(
    conversation_id: str,
    shipment_id: str,
    customer_id: Optional[int] = None,
) -> None:
    """Audit event when a shipment is successfully created in Brokerware."""
    emit(
        "shipment_created",
        {
            "conversation_id": conversation_id[:40],
            "shipment_id":     shipment_id,
            "customer_id":     str(customer_id) if customer_id else "",
        },
    )


def emit_auth_event(
    event: str,
    user: str,
    tenant_id: str = "",
    success: bool = True,
    detail: str = "",
) -> None:
    """Audit event for authentication / token validation outcomes."""
    emit(
        f"auth_{event}",
        {
            "user":      user,
            "tenant_id": tenant_id,
            "success":   str(success),
            "detail":    detail,
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_client():
    """Return a TelemetryClient, or None if App Insights is not configured."""
    global _telemetry_client, _client_initialised
    if _client_initialised:
        return _telemetry_client

    _client_initialised = True
    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn_str:
        logger.debug(
            "APPLICATIONINSIGHTS_CONNECTION_STRING not set — audit events go to log only"
        )
        return None

    try:
        from applicationinsights import TelemetryClient
        _telemetry_client = TelemetryClient(conn_str)
        logger.info("Application Insights telemetry client initialised")
    except ImportError:
        logger.warning(
            "applicationinsights package not installed — "
            "audit events will go to log only. "
            "Run: pip install applicationinsights"
        )
    except Exception as exc:
        logger.error("Failed to initialise Application Insights client: %s", exc)

    return _telemetry_client
