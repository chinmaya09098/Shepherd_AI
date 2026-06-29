"""
polling_fallback — Azure Function (Timer Trigger)

A safety net for emails that were missed by the webhook notification system:
  - Webhook subscription was temporarily expired
  - Graph API notification delivery failure
  - Message arrived before subscription was created

Polls the inbox every 15 minutes and queues any messages that:
  1. Were received in the last 30 minutes (recent window)
  2. Have NOT already been processed (checked via DedupService)

These messages are put on the same 'shepherd-email-queue' as webhook
notifications, so they flow through the exact same email_processor pipeline.

Schedule (cron): every 15 minutes → 0 */15 * * * *
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import azure.functions as func

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

logger = logging.getLogger("shepherd.polling_fallback")

_LOOKBACK_MINUTES = 30       # Check emails from the last 30 minutes
_QUEUE_NAME = os.getenv("AZURE_STORAGE_QUEUE_NAME", "shepherd-email-queue")


def main(mytimer: func.TimerRequest) -> None:
    """Timer trigger entry point — runs every 15 minutes."""
    now = datetime.now(timezone.utc)
    if mytimer.past_due:
        logger.warning("polling_fallback timer is past due")

    logger.info("polling_fallback START %s", now.isoformat())

    # ── Load dependencies ──────────────────────────────────────────────────────
    from function_app.shared.pipeline import (
        get_connection_string,
        get_graph_client,
        validate_and_get_token,
    )
    from src.services.dedup_service import DedupService

    token, access_token = validate_and_get_token()
    if not token or not access_token:
        logger.error("No Graph token available — skipping polling")
        return

    graph_client = get_graph_client()
    conn_str = get_connection_string()

    dedup = DedupService(conn_str) if conn_str else None

    # ── Poll inbox ─────────────────────────────────────────────────────────────
    lookback_time = now - timedelta(minutes=_LOOKBACK_MINUTES)

    try:
        token, messages = asyncio.run(
            graph_client.read_inbox(token, latest_message_time=lookback_time)
        )
    except Exception as exc:
        logger.error("Failed to poll inbox: %s", exc)
        return

    if not messages:
        logger.info("polling_fallback: no new messages in lookback window")
        return

    # ── Queue unprocessed messages ─────────────────────────────────────────────
    from azure.storage.queue import QueueClient
    try:
        queue_client = QueueClient.from_connection_string(conn_str, _QUEUE_NAME)
    except Exception as exc:
        logger.error("Failed to connect to queue %s: %s", _QUEUE_NAME, exc)
        return

    queued = 0
    skipped = 0

    for msg in messages:
        if not msg.id:
            continue

        # Skip if already processed via webhook
        if dedup and dedup.is_processed(msg.id):
            skipped += 1
            continue

        payload = json.dumps({
            "message_id": msg.id,
            "conversation_id": msg.conversation_id or "",
            "tenant_id": "default",
            "subscription_id": "polling_fallback",
            "received_at": now.isoformat(),
            "retry_count": 0,
        })

        try:
            queue_client.send_message(payload)
            queued += 1
            logger.info("polling_fallback queued: %s", msg.id)
        except Exception as exc:
            logger.error("Failed to queue message %s: %s", msg.id, exc)

    logger.info(
        "polling_fallback DONE: queued=%d skipped=%d total_found=%d",
        queued,
        skipped,
        len(messages),
    )
