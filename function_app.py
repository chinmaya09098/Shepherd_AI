"""
Shepherd AI — Azure Function App (v2 Python programming model).

Triggers
--------
graph_webhook       HTTP (anonymous)
    GET  — subscription validation handshake (echo validationToken query param)
    POST — receive Graph API change notifications; enqueue message IDs for processing

process_email       Queue (AzureWebJobsStorage / email-notifications queue)
    Dequeues a notification payload, fetches the full email via Graph API,
    runs the standard extraction + follow-up pipeline, and persists state.

renew_subscriptions Timer (every 47 hours)
    Calls WebhookSubscriptionManager.renew() for all active subscriptions so
    they never expire.

register_webhooks   HTTP (admin, function-level auth)
    One-shot endpoint to create the initial Graph subscription for a mailbox.
    Call this after deploying the Function App for the first time.

Environment variables (set in Function App Configuration or local.settings.json):
    AZURE_AD_TENANT_ID, AZURE_AD_CLIENT_ID, AZURE_AD_CLIENT_SECRET
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT
    AZURE_OPENAI_API_VERSION, AZURE_OPENAI_EMBEDDING_DEPLOYMENT
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY
    AZURE_STORAGE_CONNECTION_STRING, AZURE_STORAGE_LOGS_CONTAINER
    AZURE_STORAGE_QUEUE_NAME
    GRAPH_MAILBOX_USER_ID, GRAPH_WEBHOOK_NOTIFICATION_URL, GRAPH_WEBHOOK_CLIENT_STATE
    AZURE_SEARCH_CONTEXT_INDEX_NAME
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import azure.functions as func

# ---------------------------------------------------------------------------
# Azure Functions App
# ---------------------------------------------------------------------------

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger("shepherd_ai.function_app")


# ---------------------------------------------------------------------------
# Helper: lazy imports (avoids cold-start cost for unused trigger paths)
# ---------------------------------------------------------------------------

def _get_config():
    from src.config import Config
    return Config


def _get_subscription_manager():
    from src.services.webhook_subscription_manager import WebhookSubscriptionManager
    return WebhookSubscriptionManager()


def _get_orchestrator():
    from src.services.followup_orchestrator import FollowupOrchestrator
    return FollowupOrchestrator()


def _get_graph_client():
    """Build a GraphClient using app-config values (no per-user token needed here)."""
    from src.models.graph_models import GraphConfig
    from src.services.graph_client import GraphClient
    cfg = _get_config()
    graph_cfg = GraphConfig(
        tenant_id=cfg.AZURE_AD_TENANT_ID or "",
        client_id=cfg.AZURE_AD_CLIENT_ID or "",
        client_secret=cfg.AZURE_AD_CLIENT_SECRET or "",
        redirect_uri=cfg.GRAPH_REDIRECT_URI,
    )
    return GraphClient(graph_cfg)


def _get_app_token() -> str:
    from src.services.graph_client import get_app_token
    return get_app_token()


def _validate_client_state(payload: dict) -> bool:
    """
    Validate the clientState field in a Graph notification against our configured secret.
    Returns True if all notifications in the payload pass validation.
    """
    cfg       = _get_config()
    expected  = cfg.GRAPH_WEBHOOK_CLIENT_STATE
    values    = payload.get("value", [])
    for notification in values:
        received = notification.get("clientState", "")
        if received != expected:
            logger.warning(
                "clientState mismatch: expected=%r received=%r", expected, received
            )
            return False
    return True


# ---------------------------------------------------------------------------
# Trigger 1 — Graph Webhook (HTTP)
# ---------------------------------------------------------------------------

@app.route(route="graph_webhook", methods=["GET", "POST"])
def graph_webhook(req: func.HttpRequest) -> func.HttpResponse:
    """
    Receives Microsoft Graph change notifications for new inbox emails.

    GET  — Subscription validation: Microsoft sends a GET with ?validationToken=...
           The function must echo the token back within 10 seconds.

    POST — Change notification: Microsoft sends a JSON payload describing which
           resources changed. Each notification is enqueued for async processing.
    """
    # ── Validation handshake ─────────────────────────────────────────────────
    if req.method == "GET":
        validation_token = req.params.get("validationToken")
        if validation_token:
            logger.info("Graph subscription validation handshake received")
            return func.HttpResponse(
                body=validation_token,
                status_code=200,
                mimetype="text/plain",
            )
        return func.HttpResponse("Missing validationToken", status_code=400)

    # ── Change notification ───────────────────────────────────────────────────
    try:
        body = req.get_json()
    except Exception as exc:
        logger.error("Failed to parse webhook notification body: %s", exc)
        return func.HttpResponse("Bad Request", status_code=400)

    # Validate client state to reject spoofed notifications
    if not _validate_client_state(body):
        return func.HttpResponse("Forbidden — invalid clientState", status_code=403)

    cfg        = _get_config()
    queue_name = cfg.AZURE_STORAGE_QUEUE_NAME
    conn_str   = cfg.AZURE_STORAGE_CONNECTION_STRING

    if not conn_str:
        logger.error("AZURE_STORAGE_CONNECTION_STRING not configured — cannot enqueue")
        # Still return 202 so Graph does not retry; log the failure for alerting.
        return func.HttpResponse(status_code=202)

    enqueued = 0
    try:
        from azure.storage.queue import QueueClient
        queue_client = QueueClient.from_connection_string(conn_str, queue_name)

        for notification in body.get("value", []):
            resource_data = notification.get("resourceData", {})
            message_id    = resource_data.get("id", "")
            if not message_id:
                # Some notification types omit resourceData.id — skip gracefully
                continue

            envelope: Dict[str, Any] = {
                "message_id":       message_id,
                "subscription_id":  notification.get("subscriptionId"),
                "change_type":      notification.get("changeType"),
                "received_at":      datetime.now(timezone.utc).isoformat(),
            }
            queue_client.send_message(json.dumps(envelope))
            enqueued += 1
            logger.info("Enqueued message_id=%s for processing", message_id)

    except Exception as exc:
        logger.error("Error enqueuing notifications: %s", exc)
        # Return 202 anyway — Graph must not retry for transient queue errors.

    logger.info("Webhook processed: enqueued=%d notification(s)", enqueued)
    # Graph requires HTTP 202 Accepted for notification delivery confirmation.
    return func.HttpResponse(status_code=202)


# ---------------------------------------------------------------------------
# Trigger 2 — Queue processor (async email pipeline)
# ---------------------------------------------------------------------------

@app.queue_trigger(
    arg_name="msg",
    queue_name="%AZURE_STORAGE_QUEUE_NAME%",
    connection="AzureWebJobsStorage",
)
def process_email(msg: func.QueueMessage) -> None:
    """
    Dequeues a notification envelope, fetches the full email from Graph,
    runs the Shepherd AI extraction + follow-up pipeline, and persists state.

    The queue message body is a JSON string with keys:
        message_id, subscription_id, change_type, received_at
    """
    try:
        envelope = json.loads(msg.get_body().decode("utf-8"))
    except Exception as exc:
        logger.error("Failed to decode queue message: %s", exc)
        return

    message_id = envelope.get("message_id", "")
    if not message_id:
        logger.warning("Queue message missing message_id — skipping")
        return

    logger.info("Processing email message_id=%s", message_id)

    try:
        _run_email_pipeline(message_id)
    except Exception as exc:
        logger.error(
            "Unhandled error in email pipeline for message_id=%s: %s",
            message_id, exc, exc_info=True,
        )
        raise  # Re-raise so Azure Functions retries the message (up to maxDequeueCount)


def _run_email_pipeline(message_id: str) -> None:
    """
    Full Shepherd AI pipeline for one email message, executed inside the queue trigger.

    Steps:
      1.  Fetch the full message (body + attachments) from Graph using app-only token.
      2.  Detect whether the message is a customer reply to a tracked conversation.
      3.  Download & OCR-extract text from attachments via ContentUnderstandingExtractor.
      4.  Retrieve RAG context from Azure AI Search (ShipmentContextClient).
      5.  Run OpenAI extraction — Pass 1 envelope + Pass 2 per-attachment or body fallback.
      6.  Classification routing — discard spam; skip creation for tracking/status emails.
      7.  Resolve HyperionTMS customer ID via Azure AI Search + OpenAI confirmation.
      8.  Pre-submission field validation — log structural warnings.          [SOW §8]
      9.  Follow-up orchestration (handle_reply or handle_initial_extraction). [SOW §10b]
      10. HITL routing — escalate when thresholds are breached.              [SOW §7 / §9b]
      11. Auto-approval → Brokerware shipment creation + confirmation reply.  [SOW §9a/10a/12]
      12. Email record persistence to PostgreSQL + blob log upload.           [SOW §11]
    """
    import asyncio
    import re
    import tempfile

    from src.config import Config
    from src.db.email_repository import derive_class, record_email, update_email_class
    from src.extractors.content_understanding import ContentUnderstandingExtractor
    from src.extractors.openai_agent import OpenAIAgent
    from src.models.graph_models import GraphConfig
    from src.services.context_search_client import ShipmentContextClient
    from src.services.followup_orchestrator import FollowupOrchestrator
    from src.services.graph_client import GraphClient, get_app_token
    from src.services.hitl_router import HITLRouter
    from src.services.search_client import find_customer_matches
    from src.utils.blob_log_handler import BlobLogHandler

    # ── BlobLogHandler — buffer all shepherd_ai logs for this pipeline run ────
    blob_handler    = BlobLogHandler()
    shepherd_logger = logging.getLogger("shepherd_ai")
    shepherd_logger.addHandler(blob_handler)

    # State variables tracked throughout pipeline for final record / blob upload
    email_data_dict: Dict[str, Any] = {}
    email_type_val:  str             = "other"
    customer_id:     Optional[int]   = None
    pipeline_stage:  str             = "ingested"

    try:
        # ── 1. Fetch message ──────────────────────────────────────────────────
        graph_cfg = GraphConfig(
            tenant_id=Config.AZURE_AD_TENANT_ID or "",
            client_id=Config.AZURE_AD_CLIENT_ID or "",
            client_secret=Config.AZURE_AD_CLIENT_SECRET or "",
            redirect_uri=Config.GRAPH_REDIRECT_URI,
        )
        graph_client = GraphClient(graph_cfg)
        access_token = get_app_token()

        url = (
            f"https://graph.microsoft.com/v1.0"
            f"/users/{Config.GRAPH_MAILBOX_USER_ID}/messages/{message_id}"
            f"?$select=id,conversationId,subject,body,from,toRecipients,"
            f"receivedDateTime,hasAttachments"
        )
        import httpx
        with httpx.Client(timeout=30) as http_client:
            resp = http_client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            resp.raise_for_status()
            msg_data = resp.json()

        mail_message    = graph_client._parse_mail_message(msg_data)
        email_body      = (mail_message.body.content if mail_message.body else "") or ""
        email_body_text = re.sub(r"<[^>]+>", " ", email_body).strip()

        # Build a minimal email_data dict compatible with email_repository.record_email()
        sender_str = ""
        if mail_message.from_ and mail_message.from_.email_address:
            ea = mail_message.from_.email_address
            sender_str = f"{ea.name or ''} <{ea.address or ''}>".strip()

        receiver_email = ""
        to_recipients  = getattr(mail_message, "to_recipients", None) or []
        if to_recipients:
            first_to = to_recipients[0]
            if first_to and getattr(first_to, "email_address", None):
                receiver_email = first_to.email_address.address or ""

        email_data_dict = {
            "message_id":         mail_message.id,
            "conversation_id":    mail_message.conversation_id,
            "subject":            mail_message.subject,
            "from":               sender_str,
            "received_date_time": getattr(mail_message, "received_date_time", None),
            "attachments":        [],
        }

        # ── 2. Reply detection ────────────────────────────────────────────────
        orchestrator = FollowupOrchestrator()
        is_reply     = orchestrator.is_tracked_reply(mail_message)
        logger.info(
            "message_id=%s type=%s conv_id=%s",
            message_id,
            "reply" if is_reply else "new_email",
            mail_message.conversation_id or "—",
        )

        # ── 3. Download & OCR attachments ─────────────────────────────────────
        attachment_texts:    List[str]      = []
        all_attachment_texts: Optional[str] = None

        if mail_message.has_attachments:
            with tempfile.TemporaryDirectory() as tmp_dir:
                attachments = asyncio.run(
                    graph_client.download_attachments(access_token, message_id, tmp_dir)
                )
                if attachments:
                    extractor = ContentUnderstandingExtractor()
                    for att in attachments:
                        try:
                            text = extractor.extract_text(att["filepath"])
                            if text:
                                attachment_texts.append(text)
                        except Exception as exc:
                            logger.warning(
                                "OCR failed for attachment %s: %s", att["filename"], exc
                            )
            if attachment_texts:
                all_attachment_texts = "\n\n---NEXT ATTACHMENT---\n\n".join(attachment_texts)

        # ── 4. RAG context retrieval ──────────────────────────────────────────
        rag_snippets: List[str] = []
        try:
            ctx_client = ShipmentContextClient()
            query_text = f"{mail_message.subject or ''} {email_body_text[:500]}"
            rag_snippets = ctx_client.retrieve(query=query_text, top=5)
            if rag_snippets:
                logger.info("RAG context: %d snippet(s) retrieved", len(rag_snippets))
        except Exception as exc:
            logger.warning("RAG context retrieval failed (non-fatal): %s", exc)

        # ── 5. OpenAI extraction (Pass 1 envelope + Pass 2 per-attachment) ────
        agent   = OpenAIAgent()
        rag_ctx: Optional[List[str]] = rag_snippets or None

        envelope = agent.extract_email_envelope(
            email_body=email_body_text,
            all_attachment_texts=all_attachment_texts,
            rag_context=rag_ctx,
        )

        shipment = None
        if attachment_texts:
            for att_text in attachment_texts:
                s = agent.extract_shipment_data(
                    attachment_text=att_text,
                    envelope=envelope,
                    rag_context=rag_ctx,
                )
                if s:
                    shipment = s
                    break
        else:
            shipments = agent.extract_body_shipments(email_body_text, envelope)
            shipment  = shipments[0] if shipments else None

        if not shipment:
            logger.warning("No shipment extracted for message_id=%s — skipping", message_id)
            pipeline_stage = "no_extraction"
            return

        email_type_val = shipment.email_type or "other"

        # ── 6. Classification routing [SOW §5] ────────────────────────────────
        if email_type_val == "spam":
            logger.info("Spam detected — discarding message_id=%s", message_id)
            pipeline_stage = "discarded_spam"
            return

        if email_type_val in ("tracking_request", "status_update"):
            logger.info(
                "Non-tender email type=%s — no shipment creation for message_id=%s",
                email_type_val, message_id,
            )
            pipeline_stage = "classified_no_action"
            return

        # ── 7. Customer ID resolution ─────────────────────────────────────────
        try:
            sender_email_addr = ""
            if mail_message.from_ and mail_message.from_.email_address:
                sender_email_addr = mail_message.from_.email_address.address or ""

            match_result = find_customer_matches(sender_email_addr, receiver_email)
            matches      = match_result.get("matches", [])
            is_broker    = match_result.get("is_broker_match", False)

            if matches:
                if is_broker:
                    customer_id = matches[0].get("customerId")
                    logger.info("Broker domain match — customerId=%s", customer_id)
                else:
                    customer_id = agent.resolve_customer_id(
                        sender_email=sender_email_addr,
                        email_subject=mail_message.subject or "",
                        customer_list=matches,
                    )
                    logger.info("OpenAI confirmed customerId=%s", customer_id)
            else:
                logger.warning("No customer match found for sender=%s", sender_email_addr)
        except Exception as exc:
            logger.warning("Customer ID resolution failed (non-fatal): %s", exc)

        # ── 8. Pre-submission field validation [SOW §8] ───────────────────────
        _validate_shipment_fields(shipment)

        # ── 9. Follow-up orchestration [SOW §10b] ─────────────────────────────
        if is_reply:
            correlation = orchestrator.correlate_message(mail_message)
            result = asyncio.run(
                orchestrator.handle_reply(
                    reply_message=mail_message,
                    reply_shipment=shipment,
                    graph_client=graph_client,
                    access_token=access_token,
                    correlation=correlation if correlation.is_reply else None,
                )
            )
        else:
            result = asyncio.run(
                orchestrator.handle_initial_extraction(
                    shipment=shipment,
                    message=mail_message,
                    graph_client=graph_client,
                    access_token=access_token,
                )
            )

        logger.info(
            "Orchestration complete message_id=%s action=%s missing=%s",
            message_id, result.action, result.missing_fields,
        )
        pipeline_stage  = result.action
        final_shipment  = result.shipment or shipment

        # ── 10. HITL routing [SOW §7 & §9b] ──────────────────────────────────
        hitl_router = HITLRouter()
        review = hitl_router.evaluate_and_route(
            shipment=final_shipment,
            message=mail_message,
            followup_result=result,
            customer_id=customer_id,
            conversation_id=result.conversation_id or "",
            followup_count=result.followup_count,
        )

        if review:
            logger.info(
                "HITL escalation: review_id=%s reason=%s",
                review.review_id[:8], review.review_reason,
            )
            pipeline_stage = f"hitl_{review.review_reason}"
            return

        # ── 11. Auto-approval → Brokerware creation [SOW §9a / §10a / §12] ───
        if result.action in ("no_action_needed", "complete"):
            _create_and_confirm_shipment(
                shipment=final_shipment,
                message=mail_message,
                customer_id=customer_id,
                graph_client=graph_client,
                access_token=access_token,
            )
            pipeline_stage = "shipment_created"

    finally:
        # ── 12a. Email record persistence [SOW §11] ───────────────────────────
        # Strategy: try to UPDATE the row Streamlit already inserted (Phase 1).
        # If no row exists (webhook fired before Streamlit saw the email) fall
        # back to a full INSERT via record_email().
        try:
            resolved_class = derive_class(
                "Inbound",
                [email_type_val] if email_type_val else None,
            )
            updated = update_email_class(
                message_id,
                class_code=resolved_class,
                client_id=customer_id,
                has_missing_fields=pipeline_stage in ("awaiting_reply", "followup_sent"),
                failed=pipeline_stage in ("no_extraction", "failed"),
                extra_metadata={"pipeline_stage": pipeline_stage},
            )
            if not updated and email_data_dict:
                # Row not found — Streamlit hadn't loaded inbox yet; insert now.
                record_email(
                    email_data_dict,
                    direction="Inbound",
                    client_id=customer_id,
                    llm_email_types=[email_type_val] if email_type_val else None,
                    extra_metadata={
                        "pipeline_stage": pipeline_stage,
                        "message_id":     message_id,
                    },
                )
        except Exception as exc:
            logger.warning("Email record persistence failed (non-fatal): %s", exc)

        # ── 12b. Blob log upload [SOW §11] ────────────────────────────────────
        try:
            from azure.storage.blob import BlobServiceClient as _BlobSvcClient
            conn_str = Config.AZURE_STORAGE_CONNECTION_STRING
            if conn_str:
                svc       = _BlobSvcClient.from_connection_string(conn_str)
                container = getattr(Config, "AZURE_STORAGE_LOGS_CONTAINER", None) or "processed-logs"
                blob_handler.upload(
                    blob_service_client=svc,
                    container=container,
                    eml_filename=f"pipeline_{message_id}",
                )
        except Exception as exc:
            logger.warning("Blob log upload failed (non-fatal): %s", exc)
        finally:
            shepherd_logger.removeHandler(blob_handler)


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def _validate_shipment_fields(shipment) -> None:
    """
    Pre-submission structural validation [SOW Step 8].

    Logs warnings for fields that may cause downstream Brokerware API failures.
    This step does NOT block the pipeline — all issues are surfaced as warnings
    for observability and audit. The HITL router handles escalation separately.
    """
    rf = shipment.required_fields

    if not rf.pickup_location:
        logger.warning("Validation: pickup_location is null")
    elif rf.pickup_location.address:
        addr = rf.pickup_location.address
        if not addr.city and not addr.zip_code:
            logger.warning(
                "Validation: pickup_location has neither city nor zip — "
                "Brokerware submission may fail"
            )

    if not rf.drop_location:
        logger.warning("Validation: drop_location is null")
    elif rf.drop_location.address:
        addr = rf.drop_location.address
        if not addr.city and not addr.zip_code:
            logger.warning(
                "Validation: drop_location has neither city nor zip — "
                "Brokerware submission may fail"
            )

    if rf.pickup_date is None and rf.pickup_window is None:
        logger.warning("Validation: neither pickupDate nor pickupWindow is set")

    if not rf.equipment_mode:
        logger.warning("Validation: equipmentMode is null")

    if rf.items:
        weightless = [i for i in rf.items if i.weight is None]
        if weightless and rf.total_weight is None:
            logger.warning(
                "Validation: %d item(s) have no individual weight and totalWeight is null",
                len(weightless),
            )
    elif rf.total_weight is None:
        logger.warning("Validation: items list is empty and totalWeight is null")


def _create_and_confirm_shipment(
    shipment,
    message,
    customer_id: Optional[int],
    graph_client,
    access_token: str,
) -> None:
    """
    Submit the finalised shipment to Brokerware TMS and send a confirmation reply.

    Covers:
      SOW §9a  — Auto-approval path (all fields present, no HITL escalation)
      SOW §10a — Structured manifest generation + API execution
      SOW §12  — Shipment execution handed off to ShipMind via Brokerware

    Args:
        shipment:    Validated Shipment object ready for submission.
        message:     Original MailMessage (for reply threading + sender name).
        customer_id: Resolved HyperionTMS customerId, or None (falls back to default).
        graph_client: Authenticated GraphClient for sending the confirmation reply.
        access_token: Valid Graph API bearer token.
    """
    import asyncio
    from src.services.brokerware_client import create_shipment, is_configured as brokerware_configured
    from src.services.followup_email_generator import FollowupEmailGenerator

    if not brokerware_configured():
        logger.warning(
            "_create_and_confirm_shipment: Brokerware credentials not configured — skipping"
        )
        return

    # [SOW §10a / §12] Format manifest and submit to Brokerware → ShipMind
    creation_result = create_shipment(shipment, customer_id=customer_id)

    if creation_result.success:
        load_id = creation_result.shipment_id or "N/A"
        logger.info("Brokerware shipment created: loadId=%s", load_id)

        # [Natural Response Agent] Send confirmation reply within the same thread
        try:
            email_gen   = FollowupEmailGenerator()
            sender_name: Optional[str] = None
            if message.from_ and message.from_.email_address:
                raw_name    = message.from_.email_address.name or ""
                sender_name = raw_name.split()[0] if raw_name else None

            confirmation_html = email_gen.generate_confirmation_email(
                shipment=shipment,
                load_id=load_id,
                sender_name=sender_name,
                original_subject=message.subject or "",
            )
            sent = asyncio.run(
                graph_client.send_reply(
                    access_token=access_token,
                    message_id=message.id or "",
                    reply_body=confirmation_html,
                    reply_html=True,
                )
            )
            if sent:
                logger.info("Confirmation reply sent to customer for loadId=%s", load_id)
            else:
                logger.warning(
                    "Confirmation reply send failed for loadId=%s — "
                    "check Mail.Send permission on the mailbox",
                    load_id,
                )
        except Exception as exc:
            logger.error(
                "Confirmation email generation/send failed for loadId=%s: %s",
                load_id, exc,
            )
    else:
        logger.error(
            "Brokerware shipment creation failed: %s — manual intervention may be required",
            creation_result.error,
        )


# ---------------------------------------------------------------------------
# Trigger 3 — Timer: renew subscriptions every 47 hours
# ---------------------------------------------------------------------------

@app.timer_trigger(
    schedule="0 0 */47 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def renew_subscriptions(timer: func.TimerRequest) -> None:
    """
    Renews all active Graph API subscriptions before they expire.

    Runs every 47 hours. Graph allows a maximum 72-hour lifetime for
    Mail.Read subscriptions; we request 48-hour renewals to stay safe.
    """
    logger.info("Timer trigger: renewing Graph API subscriptions")

    mgr           = _get_subscription_manager()
    subscriptions = mgr.list_active()

    if not subscriptions:
        logger.info("No active subscriptions found — nothing to renew")
        return

    renewed = 0
    failed  = 0
    for sub in subscriptions:
        sub_id = sub.get("id", "")
        if not sub_id:
            continue
        if mgr.renew(sub_id):
            renewed += 1
        else:
            failed += 1

    logger.info(
        "Subscription renewal complete: renewed=%d failed=%d", renewed, failed
    )


# ---------------------------------------------------------------------------
# Trigger 4 — Admin: register webhook subscriptions (HTTP, function-level auth)
# ---------------------------------------------------------------------------

@app.route(route="register_webhooks", auth_level=func.AuthLevel.FUNCTION)
def register_webhooks(req: func.HttpRequest) -> func.HttpResponse:
    """
    One-shot admin endpoint to register a Graph change-notification subscription.

    Accepts optional JSON body:
        {
            "user_id":          "<mailbox UPN or object ID>",  // overrides env var
            "notification_url": "<Function App HTTPS URL>",    // overrides env var
            "client_state":     "<secret string>"              // overrides env var
        }

    Returns:
        200 JSON: {"subscription_id": "...", "status": "registered"}
        500 JSON: {"error": "..."}
    """
    body: Dict[str, Any] = {}
    try:
        body = req.get_json()
    except Exception:
        pass  # All params are optional — fall back to env vars

    cfg = _get_config()

    user_id          = body.get("user_id")          or cfg.GRAPH_MAILBOX_USER_ID
    notification_url = body.get("notification_url") or cfg.GRAPH_WEBHOOK_NOTIFICATION_URL
    client_state     = body.get("client_state")     or cfg.GRAPH_WEBHOOK_CLIENT_STATE

    if not user_id:
        return func.HttpResponse(
            json.dumps({"error": "user_id required (or set GRAPH_MAILBOX_USER_ID)"}),
            status_code=400,
            mimetype="application/json",
        )
    if not notification_url:
        return func.HttpResponse(
            json.dumps({"error": "notification_url required (or set GRAPH_WEBHOOK_NOTIFICATION_URL)"}),
            status_code=400,
            mimetype="application/json",
        )

    mgr    = _get_subscription_manager()
    sub_id = mgr.register(
        resource=f"users/{user_id}/mailFolders/Inbox/messages",
        change_types=["created"],
        notification_url=notification_url,
        client_state=client_state,
    )

    if sub_id:
        logger.info(
            "Webhook registered: sub_id=%s user=%s url=%s",
            sub_id, user_id, notification_url,
        )
        return func.HttpResponse(
            json.dumps({"subscription_id": sub_id, "status": "registered"}),
            status_code=200,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps({"error": "Failed to register subscription — check logs for details"}),
        status_code=500,
        mimetype="application/json",
    )
