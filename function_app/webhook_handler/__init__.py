"""
webhook_handler — Azure Function (HTTP Trigger)

Receives Microsoft Graph change notifications when a subscribed mailbox
receives a new email. Two request types:

1. Validation handshake (GET/POST with validationToken query param):
   Graph sends this when a subscription is first created. Must echo back
   the validationToken as plain text with HTTP 200.

2. Change notification (POST with JSON body):
   Contains one or more ChangeNotification objects. For each notification:
   - Validate the clientState secret
   - Extract message_id
   - Put a QueueMessage on Azure Queue Storage
   - Respond 202 Accepted (immediately — processing is async)

Function URL (set as GRAPH_NOTIFICATION_URL in App Settings):
  https://<function-app>.azurewebsites.net/api/webhook_handler

Azure Function binding (function.json):
  HTTP trigger, methods: GET POST, authLevel: function
"""
import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func

logger = logging.getLogger("shepherd.webhook_handler")

# Expected clientState secret — must match what was used when creating the subscription
_CLIENT_STATE = os.getenv("GRAPH_WEBHOOK_CLIENT_STATE", "")
_QUEUE_NAME = os.getenv("AZURE_STORAGE_QUEUE_NAME", "shepherd-email-queue")


def main(req: func.HttpRequest, outputQueue: func.Out[str]) -> func.HttpResponse:
    """
    Entry point for the webhook_handler HTTP trigger.

    Parameters (from function.json bindings):
        req:         Incoming HTTP request
        outputQueue: Azure Queue Storage output binding (shepherd-email-queue)
    """
    # ── 1. Validation handshake ────────────────────────────────────────────────
    validation_token = req.params.get("validationToken")
    if validation_token:
        logger.info("Graph subscription validation handshake received")
        return func.HttpResponse(
            body=validation_token,
            status_code=200,
            mimetype="text/plain",
        )

    # ── 2. Change notification ─────────────────────────────────────────────────
    try:
        body = req.get_json()
    except Exception as exc:
        logger.error("Failed to parse notification body: %s", exc)
        return func.HttpResponse("Bad request", status_code=400)

    notifications = body.get("value", [])
    if not notifications:
        return func.HttpResponse("No notifications", status_code=200)

    queued_count = 0

    for notification in notifications:
        try:
            # Validate clientState to prevent spoofed notifications
            client_state = notification.get("clientState", "")
            if _CLIENT_STATE and client_state != _CLIENT_STATE:
                logger.warning(
                    "clientState mismatch — discarding notification sub=%s",
                    notification.get("subscriptionId"),
                )
                continue

            # Extract message_id from resourceData.id or resource URL
            resource_data = notification.get("resourceData") or {}
            message_id = resource_data.get("id", "")
            if not message_id:
                resource = notification.get("resource", "")
                if resource:
                    message_id = resource.rstrip("/").split("/")[-1]

            if not message_id:
                logger.warning(
                    "Could not extract message_id from notification: %s",
                    json.dumps(notification)[:200],
                )
                continue

            queue_payload = json.dumps({
                "message_id": message_id,
                "conversation_id": "",
                "tenant_id": notification.get("tenantId", "default"),
                "subscription_id": notification.get("subscriptionId", ""),
                "received_at": datetime.now(timezone.utc).isoformat(),
                "retry_count": 0,
            })

            outputQueue.set(queue_payload)
            queued_count += 1
            logger.info("Queued message_id=%s", message_id)

        except Exception as exc:
            logger.error("Error processing notification: %s", exc)

    logger.info(
        "webhook_handler: processed %d notifications, queued %d",
        len(notifications),
        queued_count,
    )

    # Must respond 202 quickly — Graph retries if it doesn't get a fast response
    return func.HttpResponse(
        body=json.dumps({"queued": queued_count}),
        status_code=202,
        mimetype="application/json",
    )
