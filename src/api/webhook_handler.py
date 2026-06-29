"""
Microsoft Graph webhook notification receiver.

Exposes two routes:
  POST /webhook  — receives Graph change notifications (new email arrived)
  GET  /webhook  — receives Graph validation handshake (one-time, when subscription is created)

Flow:
  1. Shepherd AI calls WebhookService.create_subscription()
  2. Graph sends GET /webhook?validationToken=xxx  → we echo it back (within 10 s)
  3. Subscription is active. New email arrives → Graph POST /webhook
  4. We validate clientState, extract message_id, push to Azure Queue
  5. Email processor (main pipeline) consumes the queue and processes the email

Run locally:
  uvicorn src.api.webhook_handler:app --port 8502 --reload

Expose publicly (dev):
  ngrok http 8502
  → set GRAPH_NOTIFICATION_URL=https://<ngrok-id>.ngrok.io/webhook
"""
from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, Response, BackgroundTasks, HTTPException
from fastapi.responses import PlainTextResponse

from src.config import Config
from src.models.webhook_models import ChangeNotificationCollection, QueueMessage
from src.utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="Shepherd AI Webhook Handler")


# ---------------------------------------------------------------------------
# Helper — push message ID to Azure Queue Storage
# ---------------------------------------------------------------------------

async def _enqueue_message(message_id: str, subscription_id: str, tenant_id: str) -> None:
    """Push a QueueMessage to Azure Queue so the pipeline can process it."""
    if not Config.AZURE_STORAGE_CONNECTION_STRING:
        logger.warning("No AZURE_STORAGE_CONNECTION_STRING — cannot enqueue message %s", message_id)
        return

    try:
        from azure.storage.queue import QueueClient
        import base64

        queue_client = QueueClient.from_connection_string(
            Config.AZURE_STORAGE_CONNECTION_STRING,
            queue_name=Config.AZURE_STORAGE_QUEUE_NAME,
        )

        msg = QueueMessage(
            message_id=message_id,
            subscription_id=subscription_id,
            tenant_id=tenant_id,
            received_at=datetime.now(timezone.utc).isoformat(),
        )

        # Azure Queue requires base64-encoded content
        content = base64.b64encode(msg.model_dump_json().encode()).decode()
        queue_client.send_message(content)
        logger.info("Enqueued message_id=%s", message_id)

    except Exception as exc:
        logger.error("Failed to enqueue message %s: %s", message_id, exc)


# ---------------------------------------------------------------------------
# GET /webhook — Graph validation handshake
# ---------------------------------------------------------------------------

@app.get("/webhook")
async def webhook_validation(validationToken: Optional[str] = None):
    """
    Graph calls this once when a subscription is created.
    Must echo the validationToken as plain text within 10 seconds.
    """
    if not validationToken:
        raise HTTPException(status_code=400, detail="Missing validationToken")

    logger.info("Graph validation handshake received")
    return PlainTextResponse(content=validationToken, status_code=200)


# ---------------------------------------------------------------------------
# POST /webhook — incoming change notifications
# ---------------------------------------------------------------------------

@app.post("/webhook", status_code=202)
async def webhook_receiver(request: Request, background_tasks: BackgroundTasks):
    """
    Receives Graph change notifications when a new email arrives.

    Microsoft requires a 202 Accepted response within 10 seconds.
    Actual processing is offloaded to a background task / queue.
    """
    try:
        body = await request.body()
        if not body:
            return Response(status_code=202)

        data = json.loads(body)
        notifications = ChangeNotificationCollection(**data)

        expected_state = Config.GRAPH_WEBHOOK_CLIENT_STATE

        for notification in notifications.value:
            # Validate clientState to ensure the notification is from our subscription
            if notification.client_state != expected_state:
                logger.warning(
                    "clientState mismatch — got '%s', expected '%s'. Skipping.",
                    notification.client_state,
                    expected_state,
                )
                continue

            message_id = notification.get_message_id()
            subscription_id = notification.subscription_id or ""
            tenant_id = notification.tenant_id or "default"

            if not message_id:
                logger.warning("Could not extract message_id from notification: %s", notification)
                continue

            logger.info(
                "New email notification — message_id=%s subscription=%s tenant=%s",
                message_id, subscription_id, tenant_id,
            )

            # Enqueue in background so we respond to Graph within 10 seconds
            background_tasks.add_task(
                _enqueue_message, message_id, subscription_id, tenant_id
            )

    except Exception as exc:
        logger.error("webhook_receiver error: %s", exc)
        # Still return 202 — do not let Graph retry flood us
        return Response(status_code=202)

    # Must return 202 Accepted — not 200
    return Response(status_code=202)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "shepherd-webhook-handler"}
