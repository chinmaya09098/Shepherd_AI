"""
email_processor — Azure Function (Queue Trigger)

Picks up QueueMessages from 'shepherd-email-queue' and processes them
through the full Shepherd AI multi-agent pipeline:

  1. Load Graph API token from Key Vault
  2. Fetch the full email from Graph API (message_id → email_data dict)
  3. Run Orchestrator.process() → ClassificationAgent → ExtractionAgent →
     ValidationAgent → DecisionAgent → [ShipmentCreation | FollowUp | Review]
  4. On success, message is removed from queue automatically by the runtime
  5. On failure, the runtime retries up to maxDequeueCount (5) times,
     then dead-letters the message to shepherd-email-queue-poison

Queue message format (JSON):
  {
    "message_id": "AAMkAGI...",
    "conversation_id": "AAQkAD...",
    "tenant_id": "default",
    "subscription_id": "...",
    "received_at": "2025-01-01T12:00:00Z",
    "retry_count": 0
  }
"""
import json
import logging
import sys
import os

import azure.functions as func

# Allow imports from the backend root when running inside the function_app directory
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

logger = logging.getLogger("shepherd.email_processor")


def main(msg: func.QueueMessage) -> None:
    """
    Queue trigger entry point.

    Parameters:
        msg: Azure Queue Storage message (shepherd-email-queue)
    """
    raw = msg.get_body().decode("utf-8")
    logger.info("email_processor received message: %s", raw[:200])

    try:
        payload = json.loads(raw)
    except Exception as exc:
        logger.error("Failed to parse queue message: %s", exc)
        return

    message_id = payload.get("message_id", "")
    conversation_id = payload.get("conversation_id", "")
    tenant_id = payload.get("tenant_id", "default")

    if not message_id:
        logger.error("Queue message missing message_id — discarding")
        return

    # ── Load Graph token & client ──────────────────────────────────────────────
    from function_app.shared.pipeline import (
        get_graph_client,
        get_orchestrator,
        validate_and_get_token,
    )
    from src.readers.graph_email_reader import GraphEmailReader

    graph_client = get_graph_client()
    token, access_token = validate_and_get_token(tenant_id)

    if not token or not access_token:
        logger.error(
            "No Graph token available for tenant %s — cannot process message_id=%s",
            tenant_id,
            message_id,
        )
        # Raise so the runtime retries / dead-letters
        raise RuntimeError("No Graph token available")

    # ── Fetch email from Graph ─────────────────────────────────────────────────
    import asyncio
    try:
        # We need the full MailMessage object to fetch attachments
        # First get the message by ID from the inbox
        async def _fetch():
            folder_id = await graph_client.get_inbox_folder_id(access_token)
            if not folder_id:
                return None
            messages = await graph_client.get_mails_from_folder(
                access_token=access_token,
                folder_id=folder_id,
                top=1,
            )
            # Find the specific message
            for m in messages:
                if m.id == message_id:
                    return m
            # If not found via listing (e.g. already read), try fetching directly
            return None

        mail_message = asyncio.run(_fetch())
    except Exception as exc:
        logger.error("Failed to fetch mail message %s: %s", message_id, exc)
        raise

    if not mail_message:
        logger.warning(
            "Message %s not found in inbox — may have been moved/deleted",
            message_id,
        )
        return

    # ── Convert to email_data dict ─────────────────────────────────────────────
    try:
        reader = GraphEmailReader(
            graph_client=graph_client,
            attachment_output_dir=os.getenv("OUTPUT_DIR", "output"),
        )
        async def _convert():
            _, email_data = await reader.read_single_email(token, mail_message)
            return email_data

        email_data = asyncio.run(_convert())
    except Exception as exc:
        logger.error("Failed to convert message %s: %s", message_id, exc)
        raise

    if not email_data:
        logger.error("Could not convert message %s to email_data", message_id)
        raise RuntimeError("Email conversion failed")

    # ── Run the pipeline ───────────────────────────────────────────────────────
    orchestrator = get_orchestrator(access_token=access_token, graph_client=graph_client)
    if not orchestrator:
        raise RuntimeError("Orchestrator unavailable")

    result = orchestrator.process(
        email_data=email_data,
        message_id=message_id,
        conversation_id=conversation_id or mail_message.conversation_id or "",
        tenant_id=tenant_id,
    )

    logger.info(
        "email_processor done: message_id=%s decision=%s response_sent=%s",
        message_id,
        result.decision.value if result.decision else "–",
        result.response_sent,
    )

    # Raise on hard failure so runtime retries
    if result.error and result.error not in ("duplicate", "spam"):
        raise RuntimeError(f"Pipeline failed: {result.error}")
