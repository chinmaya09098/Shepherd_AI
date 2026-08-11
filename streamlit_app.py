"""
Streamlit UI for Shepherd AI POC - Email Shipment Extraction
"""
import streamlit as st
import streamlit.components.v1 as st_components
import tempfile
import json
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional
import sys

from azure.storage.blob import BlobServiceClient

# Add backend directory to Python path for imports
backend_dir = Path(__file__).parent  # streamlit_app.py is in backend/ directory
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from src.config import Config
from src.readers.eml_parser import EMLParser
from src.extractors.content_understanding import ContentUnderstandingExtractor
from src.extractors.openai_agent import OpenAIAgent
from src.models.shipment import Shipment, LocationInfo, ShipmentAddress
from src.models.client_format import format_client_json_str
# Customer matching uses Brokerware's CustomerContactsSummary (replaces the
# Hyperion + Azure AI Search vector-search path). Aliased so call sites are unchanged.
from src.services.brokerware_client import match_customer as find_customer_matches
from src.db.email_repository import record_email, update_email_class, derive_class
from src.utils.blob_log_handler import BlobLogHandler
from src.utils.logger import get_logger
from src.ui.styles import get_light_theme_css
from src.services.graph_client import GraphClient
from src.models.graph_models import GraphConfig, GraphToken, MailMessage
from src.readers.graph_email_reader import GraphEmailReader
from src.services.followup_orchestrator import FollowupOrchestrator, FollowupResult
from src.services.conversation_tracker import CorrelationResult
from src.extractors.reply_merger import ReplyMerger
from src.services.hitl_router import HITLRouter
from src.services.review_queue import ReviewQueue
from src.models.review_request import ReviewRequest, REVIEW_STATUS_PENDING
from src.services.brokerware_client import (
    create_shipment as brokerware_create_shipment,
    is_configured as brokerware_is_configured,
)

# Configure Streamlit page
st.set_page_config(
    page_title="Shepherd AI - Shipment Extractor",
    page_icon="assets/3pl_logo.png",  # Ensure this file exists
    layout="wide"
)

# Apply light theme CSS
st.markdown(get_light_theme_css(), unsafe_allow_html=True)

# Initialize logger
logger = get_logger(__name__)

# Initialize session state
if 'shipments' not in st.session_state:
    st.session_state.shipments = []
if 'email_processed' not in st.session_state:
    st.session_state.email_processed = False
if 'blob_emails' not in st.session_state:
    st.session_state.blob_emails = []
if 'email_data' not in st.session_state:
    st.session_state.email_data = None
if 'envelope' not in st.session_state:
    st.session_state.envelope = None
if 'source_breakdown' not in st.session_state:
    st.session_state.source_breakdown = None
if 'raw_cu_results' not in st.session_state:
    st.session_state.raw_cu_results = None
# Graph API session state
if 'graph_token' not in st.session_state:
    st.session_state.graph_token = None
if 'graph_connected' not in st.session_state:
    st.session_state.graph_connected = False
if 'graph_user' not in st.session_state:
    st.session_state.graph_user = None
if 'graph_messages' not in st.session_state:
    st.session_state.graph_messages = []
if 'graph_shipments' not in st.session_state:
    st.session_state.graph_shipments = []
if 'graph_email_processed' not in st.session_state:
    st.session_state.graph_email_processed = False
if 'graph_email_data' not in st.session_state:
    st.session_state.graph_email_data = None
# Follow-up email workflow state
if 'graph_current_message' not in st.session_state:
    st.session_state.graph_current_message = None       # MailMessage currently being processed
if 'followup_results' not in st.session_state:
    st.session_state.followup_results = {}              # {shipment_idx: FollowupResult}
if 'graph_is_tracked_reply' not in st.session_state:
    st.session_state.graph_is_tracked_reply = False     # True when selected email is a customer reply
# Thread-aware conversation management state
if 'graph_correlation_result' not in st.session_state:
    st.session_state.graph_correlation_result = None    # CorrelationResult for selected email
# HITL review queue state
if 'review_routed' not in st.session_state:
    st.session_state.review_routed = {}                 # {shipment_idx: review_id}
if 'show_review_queue' not in st.session_state:
    st.session_state.show_review_queue = False          # True to show review panel


def _get_blob_service_client() -> Optional[BlobServiceClient]:
    """
    Create a BlobServiceClient from configuration if available.
    """
    if not Config.AZURE_STORAGE_CONNECTION_STRING:
        logger.warning("AZURE_STORAGE_CONNECTION_STRING is not configured")
        return None
    try:
        return BlobServiceClient.from_connection_string(Config.AZURE_STORAGE_CONNECTION_STRING)
    except Exception as e:
        logger.error(f"Failed to create BlobServiceClient: {e}", exc_info=True)
        return None


def list_email_blobs(prefix: str = "sample-mails/") -> List[str]:
    """
    List .eml blobs from the configured Azure Storage container.

    By default this looks under a virtual "folder" called sample-mails/.
    If nothing is found there, it falls back to listing all .eml blobs in the container.
    """
    blob_client = _get_blob_service_client()
    container_name = Config.AZURE_STORAGE_CONTAINER_NAME

    if not blob_client or not container_name:
        return []

    try:
        container_client = blob_client.get_container_client(container_name)

        # First try with the prefix (e.g. sample-mails/)
        blobs_with_prefix = list(container_client.list_blobs(name_starts_with=prefix))
        eml_with_prefix = [b.name for b in blobs_with_prefix if b.name.lower().endswith(".eml")]
        if eml_with_prefix:
            return eml_with_prefix

        # Fallback: list all .eml blobs in the container (handles case where
        # the container itself is named 'sample-mails' and blobs are at root)
        blobs_all = container_client.list_blobs()
        return [b.name for b in blobs_all if b.name.lower().endswith(".eml")]
    except Exception as e:
        logger.error(f"Error listing blobs from container {container_name}: {e}", exc_info=True)
        return []


def _get_customer_id(shipment_data: dict) -> Optional[int]:
    """Return the pre-resolved customerId stored on the shipment dict."""
    return shipment_data.get("customer_id")


def _submit_to_brokerware(shipment: Shipment, customer_id: Optional[int] = None) -> None:
    """
    Submit a fully-extracted shipment to Brokerware TMS CreateShipmentAPI.
    Shows success/error feedback in the Streamlit UI.
    Only runs when Brokerware credentials are configured and customer is resolved.
    """
    if not brokerware_is_configured():
        return

    if shipment.email_type not in ("shipment_tender", "shipment_quote"):
        return

    # Brokerware requires a customerId to create a shipment. When the sender
    # couldn't be matched to a customer (e.g. broker-forwarded email whose sender
    # isn't in the contact list), skip creation instead of sending a raw HTTP 400.
    if not customer_id:
        st.warning(
            "Brokerware submission skipped — sender not found in Brokerware contacts. "
            "Ask your Brokerware admin to add this contact, then reprocess."
        )
        logger.warning("Brokerware submission skipped: customer_id is None")
        return

    with st.spinner("Creating shipment in Brokerware TMS..."):
        result = brokerware_create_shipment(shipment, customer_id=customer_id)

    if result.success:
        shipment_id = result.shipment_id or "N/A"
        st.success(f"Shipment created in Brokerware TMS — ID: `{shipment_id}`")
        logger.info(f"Brokerware shipment created: id={shipment_id}")
    else:
        st.error(f"Brokerware TMS — shipment creation failed: {result.error}")
        logger.error(f"Brokerware create shipment failed: {result.error}")


def _save_shipment_json_to_blob(shipment: Shipment, email_name: str, source_name: str, index: int, customer_id: Optional[int] = None) -> None:
    """
    Save a single shipment's JSON representation to the output Azure Blob container.
    """
    if shipment.email_type == "spam":
        return

    blob_client = _get_blob_service_client()
    output_container = Config.AZURE_STORAGE_OUTPUT_CONTAINER

    if not blob_client or not output_container:
        return

    try:
        container_client = blob_client.get_container_client(output_container)
        json_content = format_client_json_str(shipment, customer_id=customer_id)
        # Normalise names and build a blob path:
        # output-json/<email-name>/shipment_<index>_<attachment-name>.json
        email_folder = Path(email_name).stem.replace(" ", "_")
        attachment_part = Path(source_name).stem.replace(" ", "_")
        blob_name = f"output-json/{email_folder}/shipment_{index}_{attachment_part}.json"

        blob = container_client.get_blob_client(blob_name)
        blob.upload_blob(json_content, overwrite=True)
        logger.info(f"Saved shipment JSON to blob: {output_container}/{blob_name}")
    except Exception as e:
        logger.error(f"Error saving shipment JSON to blob: {e}", exc_info=True)


def _validate_zip_fields(shipment: Shipment) -> None:
    """
    Post-extraction check for missing zip codes.

    The LLM flags missing fields at the location level (pickupLocation /
    dropLocation). It does not flag a missing zip when the rest of the
    location is present. This function fills that gap by checking the
    zip code specifically and adding 'shipperZip' / 'consigneeZip' to
    missingRequiredFields when needed.

    Handles three cases:
      1. location exists, address exists, but zip_code is null/empty
      2. location exists, but address object itself is null (no address at all)
      3. location is null — safety net in case LLM did not flag pickupLocation
    """
    rf = shipment.required_fields

    pickup_has_zip = (
        rf.pickup_location
        and rf.pickup_location.address
        and rf.pickup_location.address.zip_code
    )
    if not pickup_has_zip:
        if "shipperZip" not in shipment.missing_required_fields:
            shipment.missing_required_fields.append("shipperZip")

    drop_has_zip = (
        rf.drop_location
        and rf.drop_location.address
        and rf.drop_location.address.zip_code
    )
    if not drop_has_zip:
        if "consigneeZip" not in shipment.missing_required_fields:
            shipment.missing_required_fields.append("consigneeZip")


def process_email_file(uploaded_file) -> List[Shipment]:
    """
    Process uploaded email file and extract shipment data
    
    Args:
        uploaded_file: Streamlit uploaded file object
        
    Returns:
        List of extracted Shipment objects
    """
    shipments = []
    
    try:
        # Validate configuration
        if not Config.validate():
            st.error("Configuration validation failed. Please check your .env file.")
            return []
        
        # Create temporary file to save uploaded email
        with tempfile.NamedTemporaryFile(delete=False, suffix='.eml') as tmp_file:
            tmp_file.write(uploaded_file.getvalue())
            tmp_path = tmp_file.name
        
        # Initialize processors
        with st.spinner("Initializing processors..."):
            eml_parser = EMLParser(output_dir=tempfile.gettempdir())
            content_extractor = ContentUnderstandingExtractor()
            openai_agent = OpenAIAgent()
        
        # Parse email
        with st.spinner("Parsing email..."):
            email_data = eml_parser.parse(tmp_path)
            st.success(f"Email parsed: {email_data['subject']}")
            st.info(f"Found {len(email_data['attachments'])} attachments")

        # Resolve customer ID via Brokerware once before processing attachments
        with st.spinner("Resolving customer ID..."):
            try:
                _cr = find_customer_matches(
                    email_data.get('from', ''), email_data.get('to', '')
                )
                _cr_matches = _cr.get("matches", [])
                resolved_cid: Optional[int] = (
                    _cr_matches[0].get("customerId") if _cr_matches else None
                )
                if resolved_cid:
                    st.info(f"Customer resolved: ID {resolved_cid}")
                else:
                    st.info("No matching customer found in Brokerware contacts")
            except Exception as _e:
                logger.warning("Customer ID resolution failed: %s", _e)
                resolved_cid = None

        # Process attachments if any, otherwise process email body
        if len(email_data['attachments']) > 0:
            _image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp', '.ico'}
            attachments = [
                a for a in email_data['attachments']
                if Path(a['filename']).suffix.lower() not in _image_exts
            ]
            filtered = len(email_data['attachments']) - len(attachments)
            msg = f"Found {len(attachments)} processable attachment(s)"
            if filtered:
                msg += f" ({filtered} inline image(s) filtered)"
            st.info(msg)

            # Pre-extract text from all attachments for envelope + Pass 2
            attachment_data = []
            for attachment in attachments:
                result = content_extractor.extract_text(attachment['filepath'])
                if result:
                    text, confidence, _raw = result
                    attachment_data.append({'attachment': attachment, 'text': text, 'confidence': confidence})
                else:
                    st.warning(f"No text extracted from {attachment['filename']}, skipping")

            # Pass 1: envelope from email body + all attachment texts combined
            all_attachment_texts = "\n\n---\n\n".join(
                f"[{d['attachment']['filename']}]\n{d['text']}" for d in attachment_data
            )
            with st.spinner("Analyzing email context..."):
                envelope = openai_agent.extract_email_envelope(
                    email_body=email_data['body'],
                    all_attachment_texts=all_attachment_texts
                )

            progress_bar = st.progress(0)
            status_text = st.empty()

            for idx, data in enumerate(attachment_data, 1):
                attachment = data['attachment']
                status_text.markdown(f'<p style="color: #FFFFFF !important; font-size: 16px; font-weight: 500;">Processing attachment {idx}/{len(attachment_data)}: {attachment["filename"]}</p>', unsafe_allow_html=True)
                progress_bar.progress(idx / len(attachment_data))

                # Pass 2: extract from attachment with envelope as read-only context
                with st.spinner(f"Extracting shipment data from {attachment['filename']}..."):
                    shipment = openai_agent.extract_shipment_data(
                        attachment_text=data['text'],
                        envelope=envelope
                    )

                if shipment:
                    shipment.nice_to_have_fields.extraction_confidence = data['confidence']
                    _validate_zip_fields(shipment)
                    shipments.append({
                        'shipment': shipment,
                        'attachment_name': attachment['filename'],
                        'attachment_index': idx,
                        'email_name': uploaded_file.name,
                        'sender_email': email_data.get('from', ''),
                    })
                    if not shipment.missing_required_fields:
                        _save_shipment_json_to_blob(
                            shipment,
                            email_name=uploaded_file.name,
                            source_name=attachment['filename'],
                            index=idx,
                            customer_id=resolved_cid,
                        )
                        _submit_to_brokerware(shipment, customer_id=resolved_cid)
                else:
                    st.warning(f"Failed to extract shipment data from {attachment['filename']}")

            progress_bar.empty()
            status_text.empty()
        else:
            # No attachments - try to extract from email body only
            st.info("No attachments found. Attempting to extract shipment data from email body...")

            with st.spinner("Extracting shipment data from email body..."):
                shipment = openai_agent.extract_shipment_data(
                    attachment_text=email_data['body'],
                    envelope=None
                )

            if shipment:
                _validate_zip_fields(shipment)
                shipments.append({
                    'shipment': shipment,
                    'attachment_name': 'Email Body',
                    'attachment_index': 1,
                    'email_name': uploaded_file.name,
                    'sender_email': email_data.get('from', ''),
                })
                if not shipment.missing_required_fields:
                    _save_shipment_json_to_blob(
                        shipment,
                        email_name=uploaded_file.name,
                        source_name='Email Body',
                        index=1,
                        customer_id=resolved_cid,
                    )
                    _submit_to_brokerware(shipment, customer_id=resolved_cid)
                st.success("Extracted shipment data from email body")
            else:
                st.warning("Could not extract shipment data from email body. The email may not contain shipment information.")
        
        # Cleanup temporary file
        Path(tmp_path).unlink(missing_ok=True)
        
        return shipments
        
    except Exception as e:
        st.error(f"Error processing email: {str(e)}")
        logger.error(f"Error processing email: {e}", exc_info=True)
        return []


def process_email_blob(blob_name: str) -> List[Shipment]:
    """
    Download an email from Azure Blob Storage and process it.

    Args:
        blob_name: Full blob path (e.g. 'sample-mails/example.eml')

    Returns:
        List of extracted Shipment objects
    """
    shipments = []

    # Attach in-memory log handler to capture all logs for this processing run
    blob_log_handler = BlobLogHandler()
    logging.getLogger().addHandler(blob_log_handler)

    try:
        if not Config.validate():
            st.error("Configuration validation failed. Please check your .env file.")
            return []

        blob_service = _get_blob_service_client()
        container_name = Config.AZURE_STORAGE_CONTAINER_NAME

        if not blob_service or not container_name:
            st.error("Azure Storage is not fully configured. Please check AZURE_STORAGE_CONNECTION_STRING and AZURE_STORAGE_CONTAINER_NAME.")
            return []

        container_client = blob_service.get_container_client(container_name)
        blob_client = container_client.get_blob_client(blob_name)

        with st.spinner(f"Downloading '{blob_name}' from Azure Blob Storage..."):
            email_bytes = blob_client.download_blob().readall()

        # Write to a temporary .eml file so we can reuse existing processing logic
        with tempfile.NamedTemporaryFile(delete=False, suffix=".eml") as tmp_file:
            tmp_file.write(email_bytes)
            tmp_path = tmp_file.name

        # Initialize processors
        with st.spinner("Initializing processors..."):
            eml_parser = EMLParser(output_dir=tempfile.gettempdir())
            content_extractor = ContentUnderstandingExtractor()
            openai_agent = OpenAIAgent()
        
        # Parse email
        with st.spinner("Parsing email..."):
            email_data = eml_parser.parse(tmp_path)
            st.session_state.email_data = email_data
            st.success(f"Email parsed from blob: {email_data['subject']}")
            st.info(f"Found {len(email_data['attachments'])} attachments")

        # Resolve customerId once for this email — available in both attachment and body paths
        with st.spinner("Resolving customer ID..."):
            try:
                sender_email = email_data.get('from', '')
                receiver_email = email_data.get('to', '')
                search_result = find_customer_matches(sender_email, receiver_email)
                matches = search_result["matches"]
                is_broker = search_result["is_broker_match"]

                if is_broker and matches:
                    # Exact broker match — use customerId directly, no need for OpenAI
                    resolved_customer_id = matches[0]["customerId"]
                    logger.info(f"Broker match resolved directly: customerId={resolved_customer_id}")
                elif matches:
                    # Unknown/direct sender — let OpenAI confirm from vector search results
                    resolved_customer_id = openai_agent.resolve_customer_id(
                        sender_email=sender_email,
                        email_subject=email_data.get('subject', ''),
                        customer_list=matches,
                    )
                else:
                    resolved_customer_id = None
            except Exception as e:
                logger.error(f"Customer ID resolution failed: {e}")
                resolved_customer_id = None

        # Process attachments if any, otherwise process email body
        if len(email_data['attachments']) > 0:
            _image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp', '.ico'}
            attachments = [
                a for a in email_data['attachments']
                if Path(a['filename']).suffix.lower() not in _image_exts
            ]
            filtered = len(email_data['attachments']) - len(attachments)
            msg = f"Found {len(attachments)} processable attachment(s)"
            if filtered:
                msg += f" ({filtered} inline image(s) filtered)"
            st.info(msg)

            # If all attachments were images, fall back to body extraction
            if not attachments:
                st.info("All attachments were inline images. Extracting shipment data from email body instead...")
                with st.spinner("Extracting shipment data from email body..."):
                    shipment = openai_agent.extract_shipment_data(
                        attachment_text=email_data['body'],
                        envelope=None
                    )
                if shipment:
                    _validate_zip_fields(shipment)
                    shipments.append({
                        'shipment': shipment,
                        'attachment_name': 'Email Body',
                        'attachment_index': 1,
                        'email_name': blob_name,
                        'customer_id': resolved_customer_id,
                    })
                    if not shipment.missing_required_fields:
                        _save_shipment_json_to_blob(
                            shipment,
                            email_name=blob_name,
                            source_name='Email Body',
                            index=1,
                            customer_id=resolved_customer_id,
                        )
                        _submit_to_brokerware(shipment, customer_id=resolved_customer_id)
                    st.success("Extracted shipment data from email body")
                else:
                    st.warning("Could not extract shipment data from email body.")
                _store_email_record(email_data, shipments, resolved_customer_id)            
                Path(tmp_path).unlink(missing_ok=True)
                return shipments

            # Pre-extract text from all attachments for envelope + Pass 2
            attachment_data = []
            raw_cu = {}
            for attachment in attachments:
                result = content_extractor.extract_text(attachment['filepath'])
                if result:
                    text, confidence, raw_result = result
                    attachment_data.append({'attachment': attachment, 'text': text, 'confidence': confidence})
                    if raw_result:
                        raw_cu[attachment['filename']] = raw_result
                else:
                    st.warning(f"No text extracted from {attachment['filename']}, skipping")
            st.session_state.raw_cu_results = raw_cu if raw_cu else None

            # Per-source structured extraction (separate OpenAI calls, display only)
            with st.spinner("Extracting structured data per source..."):
                breakdown = {}
                breakdown["emailBody"] = openai_agent.extract_source_fields(email_data['body'])
                for d in attachment_data:
                    breakdown[d['attachment']['filename']] = openai_agent.extract_source_fields(d['text'])
            st.session_state.source_breakdown = breakdown

            # Pass 1: envelope from email body + all attachment texts combined
            all_attachment_texts = "\n\n---\n\n".join(
                f"[{d['attachment']['filename']}]\n{d['text']}" for d in attachment_data
            )
            with st.spinner("Analyzing email context..."):
                envelope = openai_agent.extract_email_envelope(
                    email_body=email_data['body'],
                    all_attachment_texts=all_attachment_texts
                )
            st.session_state.envelope = envelope

            # If body has shipment data unrelated to attachments, extract it separately
            if not envelope.get('bodyRelatedToAttachments', True):
                logger.info("Body contains unrelated shipment data — running body extraction")
                with st.spinner("Extracting shipments from email body..."):
                    body_shipments = openai_agent.extract_body_shipments(email_data['body'], envelope)
                for i, shipment in enumerate(body_shipments, 1):
                    _validate_zip_fields(shipment)
                    shipments.append({
                        'shipment': shipment,
                        'attachment_name': 'Email Body',
                        'attachment_index': 0,
                        'email_name': blob_name,
                        'is_body_shipment': True,
                        'body_shipment_index': i,
                        'total_body_shipments': len(body_shipments),
                        'customer_id': resolved_customer_id,
                    })
                if body_shipments:
                    st.success(f"Extracted {len(body_shipments)} shipment(s) from email body")

            progress_bar = st.progress(0)
            status_text = st.empty()

            for idx, data in enumerate(attachment_data, 1):
                attachment = data['attachment']
                status_text.markdown(f'<p style="color: #FFFFFF !important; font-size: 16px; font-weight: 500;">Processing attachment {idx}/{len(attachment_data)}: {attachment["filename"]}</p>', unsafe_allow_html=True)
                progress_bar.progress(idx / len(attachment_data))

                # Pass 2: extract from attachment with envelope as read-only context
                with st.spinner(f"Extracting shipment data from {attachment['filename']}..."):
                    shipment = openai_agent.extract_shipment_data(
                        attachment_text=data['text'],
                        envelope=envelope
                    )

                if shipment:
                    # Fallback: prefer source breakdown items when they are more granular.
                    # source breakdown uses 'quantity' for piece count; ShipmentItem uses 'pieces'.
                    # Remap fields explicitly so nothing is lost in model_validate.
                    fallback_items = []
                    for i in (st.session_state.source_breakdown or {}).get(attachment['filename'], {}).get('items', []):
                        if not isinstance(i, dict):
                            continue
                        fallback_items.append({
                            "description": i.get("description"),
                            "pieces":      i.get("pieces") or i.get("quantity"),
                            "weight":      i.get("weight"),
                            "unit":        i.get("unit"),
                            "pallets":     i.get("pallets"),
                            "dimensions":  i.get("dimensions"),
                            "quantity":    i.get("quantity"),
                        })
                    if fallback_items and len(fallback_items) > len(shipment.required_fields.items):
                        from src.models.shipment import ShipmentItem
                        shipment.required_fields.items = [
                            ShipmentItem.model_validate(i) for i in fallback_items
                        ]
                        logger.info(f"Source breakdown fallback: {len(shipment.required_fields.items)} items for {attachment['filename']}")

                    shipment.nice_to_have_fields.extraction_confidence = data['confidence']
                    _validate_zip_fields(shipment)
                    shipments.append({
                        'shipment': shipment,
                        'attachment_name': attachment['filename'],
                        'attachment_index': idx,
                        'email_name': blob_name,
                        'customer_id': resolved_customer_id,
                    })
                    if not shipment.missing_required_fields:
                        _save_shipment_json_to_blob(
                            shipment,
                            email_name=blob_name,
                            source_name=attachment['filename'],
                            index=idx,
                            customer_id=resolved_customer_id,
                        )
                        _submit_to_brokerware(shipment, customer_id=resolved_customer_id)
                else:
                    st.warning(f"Failed to extract shipment data from {attachment['filename']}")

            progress_bar.empty()
            status_text.empty()
        else:
            st.info("No attachments found. Attempting to extract shipment data from email body...")

            with st.spinner("Extracting shipment data from email body..."):
                shipment = openai_agent.extract_shipment_data(
                    attachment_text=email_data['body'],
                    envelope=None
                )

            if shipment:
                _validate_zip_fields(shipment)
                shipments.append({
                    'shipment': shipment,
                    'attachment_name': 'Email Body',
                    'attachment_index': 1,
                    'email_name': blob_name,
                    'customer_id': resolved_customer_id,
                })
                if not shipment.missing_required_fields:
                    _save_shipment_json_to_blob(
                        shipment,
                        email_name=blob_name,
                        source_name='Email Body',
                        index=1,
                        customer_id=resolved_customer_id,
                    )
                    _submit_to_brokerware(shipment, customer_id=resolved_customer_id)
                st.success("Extracted shipment data from email body")
            else:
                st.warning("Could not extract shipment data from email body. The email may not contain shipment information.")

        Path(tmp_path).unlink(missing_ok=True)

        # Persist a single email record for this inbound email
        _store_email_record(email_data, shipments, resolved_customer_id)

        # Upload captured logs to $logs/Processed_Logs/<eml_name>.log
        log_path = blob_log_handler.upload(blob_service, Config.AZURE_STORAGE_LOGS_CONTAINER, blob_name)
        if log_path:
            logger.info(f"Processing log saved to $logs/{log_path}")

        return shipments

    except Exception as e:
        st.error(f"Error processing blob email: {str(e)}")
        logger.error(f"Error processing blob email {blob_name}: {e}", exc_info=True)
        blob_log_handler.upload(blob_service, Config.AZURE_STORAGE_LOGS_CONTAINER, blob_name)
        return []

    finally:
        logging.getLogger().removeHandler(blob_log_handler)


def format_shipment_json(shipment: Shipment) -> str:
    return shipment.model_dump_json(by_alias=True, exclude_none=True, indent=2)


def _store_email_record(email_data: dict, shipments: List[dict], customer_id: Optional[int]) -> None:
    """
    Persist one row in PostgreSQL for the inbound email just processed.

    Phase 1 (upsert_inbox_email) already inserts a 'pending' row when the inbox
    loads. Phase 2 (here) should UPDATE that row with the resolved class, client_id,
    and status. Falls back to INSERT only if no row exists yet (e.g. webhook path).

    The 'Class' (SM/CM/AI) is derived from the per-attachment email types so
    shipment-bearing mail is tagged 'SM' and everything else 'CM'. Failures are
    swallowed — this never blocks extraction.
    """
    try:
        llm_email_types = [s["shipment"].email_type for s in (shipments or [])]
        message_id = (
            email_data.get("message_id")
            or email_data.get("internet_message_id")
        )
        class_code = derive_class("Inbound", llm_email_types)
        extra = {"shipment_count": len(shipments or [])}

        # Try UPDATE first (row already exists from Phase 1 inbox load)
        if message_id:
            updated = update_email_class(
                message_id,
                class_code=class_code,
                client_id=customer_id,
                extra_metadata=extra,
            )
            if updated:
                return  # Row patched — done

        # No existing row — INSERT (webhook or first-time processing)
        record_email(
            email_data,
            direction="Inbound",
            client_id=customer_id,
            customer_id=customer_id,
            llm_email_types=llm_email_types,
            extra_metadata=extra,
        )
    except Exception as e:
        logger.error(f"Failed to store email record: {e}")



# ---------------------------------------------------------------------------
# Microsoft Graph API helpers
# ---------------------------------------------------------------------------

def run_async(coro):
    """Run an async coroutine synchronously from Streamlit's sync context."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # If there is already a running event loop (e.g. Jupyter), fall back to a thread.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()


def _init_graph_client() -> Optional[GraphClient]:
    """Create a GraphClient from Config. Returns None if Graph API is not configured."""
    if not all([Config.AZURE_AD_TENANT_ID, Config.AZURE_AD_CLIENT_ID, Config.AZURE_AD_CLIENT_SECRET]):
        return None
    config = GraphConfig(
        tenant_id=Config.AZURE_AD_TENANT_ID,
        client_id=Config.AZURE_AD_CLIENT_ID,
        client_secret=Config.AZURE_AD_CLIENT_SECRET,
        redirect_uri=Config.GRAPH_REDIRECT_URI,
    )
    return GraphClient(config)


def process_graph_email(email_data: dict, message_id: str, submit_shipments: bool = True) -> List[dict]:
    """
    Process a Graph API email dict through the full extraction pipeline.

    Equivalent to process_email_blob() but takes an already-converted email_data
    dict from GraphEmailReader instead of downloading from Azure Blob Storage.

    Args:
        email_data: EMLParser-compatible dict from GraphEmailReader._convert_message()
        message_id: Graph message ID (used for naming saved outputs)
        submit_shipments: When False, extract only and skip Brokerware submission.
            Used for tracked replies, where the shipment must be created from the
            MERGED thread data (after handle_reply), not this single email.

    Returns:
        List of shipment dicts (same schema as process_email_blob)
    """
    shipments = []
    email_name = email_data.get('subject', message_id) or message_id

    try:
        if not Config.validate():
            st.error("Configuration validation failed. Please check your .env file.")
            return []

        with st.spinner("Initializing processors..."):
            content_extractor = ContentUnderstandingExtractor()
            openai_agent = OpenAIAgent()

        st.session_state.graph_email_data = email_data
        st.info(f"Found {len(email_data.get('attachments', []))} attachment(s)")

        # Resolve customer ID
        with st.spinner("Resolving customer ID..."):
            try:
                sender_email = email_data.get('from', '')
                receiver_email = email_data.get('to', '')
                search_result = find_customer_matches(sender_email, receiver_email)
                matches = search_result["matches"]
                is_broker = search_result["is_broker_match"]

                if is_broker and matches:
                    resolved_customer_id = matches[0]["customerId"]
                elif matches:
                    resolved_customer_id = openai_agent.resolve_customer_id(
                        sender_email=sender_email,
                        email_subject=email_data.get('subject', ''),
                        customer_list=matches,
                    )
                else:
                    resolved_customer_id = None
            except Exception as e:
                logger.error(f"Customer ID resolution failed: {e}")
                resolved_customer_id = None

        attachments = email_data.get('attachments', [])

        if attachments:
            # Pre-extract text from all attachments — run OCR in parallel
            attachment_data = []
            raw_cu = {}
            with st.spinner(f"Extracting text from {len(attachments)} attachment(s) in parallel..."):
                def _ocr_one(att):
                    return att, content_extractor.extract_text(att['filepath'])

                with ThreadPoolExecutor(max_workers=min(len(attachments), 5)) as pool:
                    futures = {pool.submit(_ocr_one, att): att for att in attachments}
                    for future in as_completed(futures):
                        att, result = future.result()
                        if result:
                            text, confidence, raw_result = result
                            attachment_data.append({'attachment': att, 'text': text, 'confidence': confidence})
                            if raw_result:
                                raw_cu[att['filename']] = raw_result
                        else:
                            st.warning(f"No text extracted from {att['filename']}, skipping")
            # Restore original attachment order
            order = {att['filename']: i for i, att in enumerate(attachments)}
            attachment_data.sort(key=lambda d: order.get(d['attachment']['filename'], 999))
            st.session_state.raw_cu_results = raw_cu if raw_cu else None

            # Per-source structured extraction — run all OpenAI calls in parallel
            with st.spinner("Extracting structured data from all sources in parallel..."):
                sources = [("emailBody", email_data['body'])] + [
                    (d['attachment']['filename'], d['text']) for d in attachment_data
                ]
                breakdown = {}
                def _extract_source(name_text):
                    name, text = name_text
                    return name, openai_agent.extract_source_fields(text)

                with ThreadPoolExecutor(max_workers=min(len(sources), 6)) as pool:
                    for name, fields in pool.map(_extract_source, sources):
                        breakdown[name] = fields
            st.session_state.source_breakdown = breakdown

            # Pass 1: envelope
            all_attachment_texts = "\n\n---\n\n".join(
                f"[{d['attachment']['filename']}]\n{d['text']}" for d in attachment_data
            )
            with st.spinner("Analyzing email context..."):
                envelope = openai_agent.extract_email_envelope(
                    email_body=email_data['body'],
                    all_attachment_texts=all_attachment_texts
                )
            st.session_state.envelope = envelope

            # Body shipments unrelated to attachments
            if not envelope.get('bodyRelatedToAttachments', True):
                with st.spinner("Extracting shipments from email body..."):
                    body_shipments = openai_agent.extract_body_shipments(email_data['body'], envelope)
                for i, shipment in enumerate(body_shipments, 1):
                    _validate_zip_fields(shipment)
                    shipments.append({
                        'shipment': shipment,
                        'attachment_name': 'Email Body',
                        'attachment_index': 0,
                        'email_name': email_name,
                        'is_body_shipment': True,
                        'body_shipment_index': i,
                        'total_body_shipments': len(body_shipments),
                        'customer_id': resolved_customer_id,
                    })
                if body_shipments:
                    st.success(f"Extracted {len(body_shipments)} shipment(s) from email body")

            # Pass 2: per-attachment extraction — run all in parallel
            with st.spinner(f"Extracting shipment data from {len(attachment_data)} attachment(s) in parallel..."):
                def _extract_shipment(idx_data):
                    idx, data = idx_data
                    return idx, data, openai_agent.extract_shipment_data(
                        attachment_text=data['text'],
                        envelope=envelope
                    )

                with ThreadPoolExecutor(max_workers=min(len(attachment_data), 5)) as pool:
                    extraction_results = list(pool.map(_extract_shipment, enumerate(attachment_data, 1)))
                extraction_results.sort(key=lambda x: x[0])

            for idx, data, shipment in extraction_results:
                attachment = data['attachment']

                if shipment:
                    fallback_items = []
                    for i in (st.session_state.source_breakdown or {}).get(attachment['filename'], {}).get('items', []):
                        if not isinstance(i, dict):
                            continue
                        fallback_items.append({
                            "description": i.get("description"),
                            "pieces": i.get("pieces") or i.get("quantity"),
                            "weight": i.get("weight"),
                            "unit": i.get("unit"),
                            "pallets": i.get("pallets"),
                            "dimensions": i.get("dimensions"),
                            "quantity": i.get("quantity"),
                        })
                    if fallback_items and len(fallback_items) > len(shipment.required_fields.items):
                        from src.models.shipment import ShipmentItem
                        shipment.required_fields.items = [
                            ShipmentItem.model_validate(i) for i in fallback_items
                        ]

                    shipment.nice_to_have_fields.extraction_confidence = data['confidence']
                    _validate_zip_fields(shipment)
                    shipments.append({
                        'shipment': shipment,
                        'attachment_name': attachment['filename'],
                        'attachment_index': idx,
                        'email_name': email_name,
                        'customer_id': resolved_customer_id,
                    })
                    if not shipment.missing_required_fields:
                        _save_shipment_json_to_blob(
                            shipment,
                            email_name=email_name,
                            source_name=attachment['filename'],
                            index=idx,
                            customer_id=resolved_customer_id,
                        )
                        if submit_shipments:
                            _submit_to_brokerware(shipment, customer_id=resolved_customer_id)
                else:
                    st.warning(f"Failed to extract shipment data from {attachment['filename']}")

        else:
            st.info("No attachments found. Attempting to extract shipment data from email body...")
            with st.spinner("Extracting shipment data from email body..."):
                shipment = openai_agent.extract_shipment_data(
                    attachment_text=email_data['body'],
                    envelope=None
                )
            if shipment:
                _validate_zip_fields(shipment)
                shipments.append({
                    'shipment': shipment,
                    'attachment_name': 'Email Body',
                    'attachment_index': 1,
                    'email_name': email_name,
                    'customer_id': resolved_customer_id,
                })
                if not shipment.missing_required_fields:
                    _save_shipment_json_to_blob(
                        shipment,
                        email_name=email_name,
                        source_name='Email Body',
                        index=1,
                        customer_id=resolved_customer_id,
                    )
                    if submit_shipments:
                        _submit_to_brokerware(shipment, customer_id=resolved_customer_id)
                st.success("Extracted shipment data from email body")
            else:
                st.warning("Could not extract shipment data from email body. The email may not contain shipment information.")

        # Persist a single email record for this inbound Graph email
        _store_email_record(email_data, shipments, resolved_customer_id)

        return shipments

    except Exception as e:
        st.error(f"Error processing Graph email: {str(e)}")
        logger.error(f"Error processing Graph email: {e}", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Shipment email domains / senders / subject keywords
# ---------------------------------------------------------------------------

# Sender domains that are never logistics emails
_NON_SHIPMENT_DOMAINS = {
    "microsoft.com", "linkedin.com", "facebook.com", "twitter.com",
    "instagram.com", "youtube.com", "google.com", "amazon.com",
    "apple.com", "github.com", "slack.com", "zoom.us", "dropbox.com",
    "salesforce.com", "hubspot.com", "mailchimp.com", "constantcontact.com",
    "sendgrid.net", "exacttarget.com", "marketo.com",
}

# Subject-line fragments that clearly indicate non-shipment email
_NON_SHIPMENT_SUBJECT_KEYWORDS = [
    "microsoft teams", "teams meeting", "calendar invite", "meeting invite",
    "you're invited", "join the meeting",
    "newsletter", "unsubscribe", "promotional", "special offer", "deal of",
    "% off", "save now", "limited time",
    "verify your email", "confirm your email", "reset your password",
    "security alert", "sign in to", "your account",
    "welcome to microsoft", "office 365", "microsoft 365", "azure portal",
    "onedrive", "outlook", "sharepoint", "get started with",
    "productivity with", "work together", "organize your day",
    "linkedin", "facebook", "twitter",
    "invoice from", "receipt from",                # accounting, not freight
]

# Subject-line fragments that strongly indicate a shipment email
_SHIPMENT_SUBJECT_KEYWORDS = [
    "load", "shipment", "freight", "tender", "pickup", "delivery",
    "bol ", "bill of lading", " ltl", " ftl", " tl ",
    "truck", "carrier", "haul", "dispatch",
    "rate", "quote", "ratecon", "rate con",
    "cargo", "shipper", "consignee",
    "tracking", "ship via",
    "carrierpoint", "brokerware", "shipmind",
    "booking", "booking confirmation",
    "customer:",                                    # forwarded load tenders (e.g. "Fw: Customer: ...")
    "pu ", "p/u", "del ", "d/o",                  # shorthand used in freight
    "fw:", "fwd:", "re:",                           # forwarded / replied freight threads
]

# Sender-email fragments that reliably indicate logistics
_SHIPMENT_SENDER_FRAGMENTS = [
    "carrierpoint", "brokerware", "shipmind", "ecogistics",
    "freight", "logistics", "trucking", "carrier", "dispatch",
    "shipping", "transport",
]


def _heuristic_shipment_verdict(msg: MailMessage) -> Optional[bool]:
    """
    Fast, free pre-filter on subject/body-preview/sender.

    Returns:
      True  — clearly shipment-related (strong keyword or logistics sender)
      False — clearly NOT shipment (known marketing/system sender or subject)
      None  — ambiguous; the caller should defer to the AI classifier
    """
    subject  = (msg.subject or "").lower()
    preview  = (msg.body_preview or "").lower()
    sender   = ""
    sender_domain = ""
    if msg.from_ and msg.from_.email_address:
        sender = (msg.from_.email_address.address or "").lower()
        sender_domain = sender.split("@")[-1] if "@" in sender else ""

    # ── Clearly NOT shipment ─────────────────────────────────────────────
    if sender_domain in _NON_SHIPMENT_DOMAINS:
        return False
    if any(s in sender for s in ("noreply@", "no-reply@", "donotreply@", "mailer-daemon@")):
        return False
    if any(kw in subject for kw in _NON_SHIPMENT_SUBJECT_KEYWORDS):
        return False

    # ── Clearly shipment ─────────────────────────────────────────────────
    combined = subject + " " + preview
    if any(kw in combined for kw in _SHIPMENT_SUBJECT_KEYWORDS):
        return True
    if any(frag in sender for frag in _SHIPMENT_SENDER_FRAGMENTS):
        return True

    # ── Ambiguous — let the AI classifier decide ─────────────────────────
    return None


def _msg_sender(msg: MailMessage) -> str:
    if msg.from_ and msg.from_.email_address:
        return (msg.from_.email_address.address or "")
    return ""


def _filter_shipment_emails(messages: List[MailMessage]) -> List[MailMessage]:
    """
    Filter to shipment-related emails using a hybrid strategy:
      1. Fast keyword/sender heuristic decides the obvious cases for free.
      2. Ambiguous emails are sent to the backend AI classifier
         (OpenAIAgent.classify_email) — judged from subject + body preview only,
         no attachment OCR — so the same taxonomy as full extraction is used.

    AI verdicts are cached per message id in session_state so Streamlit reruns
    don't re-classify. On any AI error we fail open (show the email) rather than
    risk hiding a genuine freight email.
    """
    cache: dict = st.session_state.setdefault("_email_class_cache", {})
    agent: Optional[OpenAIAgent] = None
    kept: List[MailMessage] = []

    for m in messages:
        verdict = _heuristic_shipment_verdict(m)

        if verdict is None:  # ambiguous → AI classifier (cached)
            key = m.id or f"{m.subject}|{_msg_sender(m)}"
            if key in cache:
                verdict = cache[key]
            else:
                try:
                    if agent is None:
                        agent = OpenAIAgent()
                    verdict = agent.is_shipment_email(
                        m.subject or "", m.body_preview or "", _msg_sender(m)
                    )
                except Exception as e:
                    logger.warning("AI email classification failed (%s) — showing by default", e)
                    verdict = True  # fail open: don't hide a possibly-real freight email
                cache[key] = verdict

        if verdict:
            kept.append(m)

    return kept


_CORRELATION_METHOD_LABELS = {
    "conversation_id": "Conversation Thread ID",
    "reference_id":    "Reference / Order ID",
    "subject":         "Subject-Line Similarity",
    "sender+subject":  "Sender + Subject Match",
    "sender":          "Sender Email Address",
    "none":            "Not Matched",
}

_STATUS_COLORS = {
    "processing":          ("🔵", "Processing"),
    "awaiting_reply":      ("🟡", "Awaiting Reply"),
    "complete":            ("🟢", "Complete"),
    "max_retries_reached": ("🔴", "Max Retries — Manual Review"),
    "failed":              ("🔴", "Failed"),
}

_EVENT_ICONS = {
    "email_ingested":        "📨",
    "email_classified":      "🏷️",
    "extraction_complete":   "🔍",
    "customer_id_resolved":  "👤",
    "followup_generated":    "✍️",
    "followup_sent":         "📤",
    "reply_received":        "📩",
    "reply_correlated":      "🔗",
    "data_merged":           "🔀",
    "validation_passed":     "✅",
    "conversation_complete": "🏁",
    "max_retries_reached":   "⛔",
    "error":                 "❌",
}


def _render_conversation_timeline():
    """
    Render the conversation lifecycle timeline for the currently selected email.
    Shows the full event log from ConversationState (inline) plus correlation info.
    Only displayed when a tracked conversation is associated with the current email.
    """
    current_msg: Optional[MailMessage] = st.session_state.get("graph_current_message")
    corr: Optional[CorrelationResult]  = st.session_state.get("graph_correlation_result")

    if not current_msg:
        return

    # Try to load conversation state by conversation_id first, then via correlation
    conv_state = None
    try:
        _orch = FollowupOrchestrator()
        if current_msg.conversation_id:
            conv_state = _orch.get_conversation_state(current_msg.conversation_id)
        if conv_state is None and corr and corr.state:
            conv_state = corr.state
    except Exception:
        pass

    if not conv_state:
        return

    icon, status_label = _STATUS_COLORS.get(conv_state.status, ("⚪", conv_state.status))
    with st.expander(f"Conversation History  {icon} {status_label}", expanded=False):
        # Metadata row
        meta_cols = st.columns([3, 2, 2, 2])
        meta_cols[0].markdown(f"**Thread:** `{conv_state.conversation_id[:32]}…`")
        meta_cols[1].markdown(f"**Follow-ups sent:** {conv_state.followup_count} / {conv_state.max_followups}")
        meta_cols[2].markdown(f"**Status:** {status_label}")

        # Correlation method (only when this is a reply)
        if corr and corr.is_reply and corr.method != "none":
            method_label = _CORRELATION_METHOD_LABELS.get(corr.method, corr.method)
            meta_cols[3].markdown(f"**Matched via:** {method_label} ({corr.confidence:.0%})")

        if conv_state.reference_ids:
            st.caption(f"Reference IDs tracked: {', '.join(conv_state.reference_ids)}")

        st.divider()

        # Lifecycle events
        events = conv_state.lifecycle_events
        if events:
            for ev in events:
                ev_key    = ev.get("event", "")
                ev_icon   = _EVENT_ICONS.get(ev_key, "•")
                ev_label  = ev_key.replace("_", " ").title()
                ts_raw    = ev.get("timestamp", "")
                try:
                    from datetime import datetime as _dt
                    ts_fmt = _dt.fromisoformat(ts_raw).strftime("%b %d, %H:%M UTC")
                except Exception:
                    ts_fmt = ts_raw[:16]
                details   = ev.get("details", {})
                detail_md = "  ·  " + ",  ".join(
                    f"`{k}`: {v}" for k, v in details.items() if v is not None and v != []
                ) if details else ""
                st.markdown(f"{ev_icon} **{ts_fmt}** — {ev_label}{detail_md}")
        else:
            st.caption("No lifecycle events recorded yet.")

        if conv_state.missing_fields:
            st.markdown(f"**Still missing:** {', '.join(f'`{f}`' for f in conv_state.missing_fields)}")


def _render_active_conversations_panel():
    """
    Sidebar panel showing all conversations currently awaiting a customer reply.
    Displayed in the graph tab so the user knows which threads are open.
    """
    try:
        _orch  = FollowupOrchestrator()
        active = _orch.list_active_conversations()
    except Exception:
        return

    if not active:
        return

    with st.sidebar:
        st.markdown("---")
        # Review queue badge
        try:
            _rq_sidebar = ReviewQueue()
            _rq_count   = len(_rq_sidebar.get_pending())
            if _rq_count:
                st.error(f"🔴 Review Queue: **{_rq_count}** pending")
                if st.button("View Review Queue", key="sidebar_open_rq", use_container_width=True):
                    st.session_state.show_review_queue = True
                    st.rerun()
        except Exception:
            pass

        st.markdown(f"**Open Conversations ({len(active)})**")
        for state in active:
            icon, _ = _STATUS_COLORS.get(state.status, ("⚪", ""))
            sent_at = (state.last_followup_at or state.created_at or "")[:10]
            st.markdown(
                f"{icon} **{state.sender_email}**  \n"
                f"{state.subject[:50]}{'…' if len(state.subject) > 50 else ''}  \n"
                f"Follow-up #{state.followup_count} · {sent_at}  \n"
                f"Missing: {', '.join(f'`{f}`' for f in state.missing_fields)}"
            )
            st.markdown("")


def _render_graph_tab():
    """Render the Live Mailbox (Microsoft Graph) tab content."""

    # ── Handle OAuth callback (Microsoft redirects back with ?code=...) ──────
    params = st.query_params
    auth_code = params.get("code")
    auth_state = params.get("state")

    if auth_code and auth_state in ("add", "update") and not st.session_state.graph_connected:
        client = _init_graph_client()
        if client:
            with st.spinner("Completing Microsoft sign-in..."):
                try:
                    token = run_async(client.exchange_code_for_tokens(auth_code))
                    user_profile = run_async(client.get_user_profile(token.access_token))
                    if user_profile is None:
                        st.error(
                            "Sign-in token was obtained but your user profile could not be "
                            "fetched from Microsoft Graph (/me). Check that:\n"
                            "1. The Azure App Registration has **User.Read** delegated permission with admin consent.\n"
                            "2. The deployed server has outbound HTTPS access to graph.microsoft.com.\n"
                            "3. **GRAPH_REDIRECT_URI** matches the URI registered in Azure."
                        )
                        logger.error(
                            "get_user_profile returned None — User.Read scope may be missing "
                            "or graph.microsoft.com is unreachable from the deployed server."
                        )
                        st.query_params.clear()
                        return
                    st.session_state.graph_token = token
                    st.session_state.graph_connected = True
                    st.session_state.graph_user = (
                        user_profile.user_principal_name or user_profile.id or "Unknown"
                    )
                    st.query_params.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Microsoft sign-in failed: {e}")
                    logger.error(f"Graph OAuth callback error: {e}", exc_info=True)
                    st.query_params.clear()
                    return

    # ── Check Graph API configuration ─────────────────────────────────────────
    graph_configured = all([
        Config.AZURE_AD_TENANT_ID and Config.AZURE_AD_TENANT_ID != "your-tenant-id",
        Config.AZURE_AD_CLIENT_ID and Config.AZURE_AD_CLIENT_ID != "your-client-id",
        Config.AZURE_AD_CLIENT_SECRET and Config.AZURE_AD_CLIENT_SECRET != "your-client-secret",
    ])

    if not graph_configured:
        st.warning(
            "Microsoft Graph API credentials are not configured. "
            "Add **AZURE_AD_TENANT_ID**, **AZURE_AD_CLIENT_ID**, and **AZURE_AD_CLIENT_SECRET** "
            "to your `.env` file to enable live mailbox access."
        )
        with st.expander("Configuration guide"):
            st.markdown("""
**Required `.env` variables:**
```
AZURE_AD_TENANT_ID=<your Azure AD tenant ID>
AZURE_AD_CLIENT_ID=<App Registration client/application ID>
AZURE_AD_CLIENT_SECRET=<App Registration client secret>
GRAPH_REDIRECT_URI=http://localhost:8501
```

**Steps:**
1. Create an App Registration in Azure Active Directory
2. Add Delegated permissions: `Mail.Read`, `Mail.ReadWrite`, `Mail.Send`, `User.Read`
3. Grant admin consent for the permissions
4. Add `http://localhost:8501` as a Redirect URI (Web platform)
5. Copy the Tenant ID, Client ID, and Client Secret into `.env`
""")
        return

    client = _init_graph_client()

    # ── Not connected: show Sign In button ────────────────────────────────────
    if not st.session_state.graph_connected:
        st.info(
            "Connect your Microsoft mailbox to read live emails directly. "
            "You will be redirected to Microsoft's sign-in page and returned here after authentication."
        )
        st.markdown(
            "**Redirect URI** (must be registered in your Azure App Registration as a Web redirect URI):"
        )
        st.code(Config.GRAPH_REDIRECT_URI, language=None)

        auth_url = client.get_auth_url(state="add")
        st.markdown(
            f'<a href="{auth_url}" target="_self" style="display:inline-block;padding:10px 22px;'
            f'background-color:#0078d4;color:white;text-decoration:none;border-radius:6px;'
            f'font-weight:600;font-size:0.95rem;">Sign in with Microsoft</a>',
            unsafe_allow_html=True,
        )
        return

    # ── Connected ─────────────────────────────────────────────────────────────
    col_status, col_disconnect = st.columns([4, 1])
    with col_status:
        st.success(f"Connected as: **{st.session_state.graph_user}**")
    with col_disconnect:
        if st.button("Disconnect", key="graph_disconnect"):
            st.session_state.graph_token = None
            st.session_state.graph_connected = False
            st.session_state.graph_user = None
            st.session_state.graph_messages = []
            st.session_state.graph_shipments = []
            st.session_state.graph_email_processed = False
            st.session_state.graph_email_data = None
            st.session_state.graph_current_message = None
            st.session_state.followup_results = {}
            st.session_state.graph_is_tracked_reply = False
            st.session_state.graph_correlation_result = None
            st.rerun()

    # Sidebar: show open conversations awaiting reply.
    # Hide it once a follow-up has been sent so the main content gets full width.
    # The sidebar comes back automatically when the user selects a new email
    # (followup_results is reset to {} on every new email selection).
    _followup_sent = any(
        r and r.action in ("followup_sent", "complete")
        for r in st.session_state.followup_results.values()
    )
    if not _followup_sent:
        _render_active_conversations_panel()

    # Load inbox only on first render or when user clicks Refresh — avoids
    # re-fetching from Graph API on every Streamlit rerender (saves 2-5s per interaction).
    col_refresh, col_status = st.columns([1, 5])
    with col_refresh:
        refresh_inbox = st.button("Refresh Inbox", key="graph_refresh_inbox")
    with col_status:
        if st.session_state.graph_messages:
            st.caption(f"{len(st.session_state.graph_messages)} message(s) loaded")

    if not st.session_state.graph_messages or refresh_inbox:
        with st.spinner("Loading inbox..."):
            try:
                updated_token, messages = run_async(
                    client.read_inbox(st.session_state.graph_token)
                )
                st.session_state.graph_token = updated_token
                st.session_state.graph_messages = messages

                # ── Phase 1: immediately store every inbox email to PostgreSQL ────
                # class_code is NULL (unprocessed) at this point.
                # update_email_class() will patch it once the pipeline runs.
                try:
                    from src.db.email_repository import upsert_inbox_email
                    for _m in (st.session_state.graph_messages or []):
                        upsert_inbox_email(_m)
                except Exception as _pe:
                    logger.warning("Inbox early-store to PostgreSQL failed (non-fatal): %s", _pe)

            except Exception as e:
                st.error(f"Failed to load inbox: {e}")
                logger.error(f"Graph inbox load error: {e}", exc_info=True)

    # ── Email list and processing ─────────────────────────────────────────────
    if st.session_state.graph_messages:
        all_messages: List[MailMessage] = st.session_state.graph_messages

        # ── Shipment email filter ─────────────────────────────────────────────
        messages = _filter_shipment_emails(all_messages)

        if not messages:
            return

        def _fmt_msg(idx: int) -> str:
            msg = messages[idx]
            from_str = ""
            if msg.from_ and msg.from_.email_address:
                ea = msg.from_.email_address
                from_str = ea.name or ea.address or ""
            date_str = ""
            if msg.received_date_time:
                date_str = msg.received_date_time.strftime("%b %d, %Y %H:%M")
            subject = msg.subject or "(No subject)"
            att_flag = "  [+att]" if msg.has_attachments else ""
            unread = "● " if not msg.is_read else "  "
            return f"{unread}{date_str}  |  {from_str}  |  {subject}{att_flag}"

        selected_idx = st.selectbox(
            "Select an email to process:",
            options=range(len(messages)),
            format_func=_fmt_msg,
            key="graph_email_select",
        )

        col_proc, _ = st.columns([1, 4])
        with col_proc:
            process_graph_btn = st.button(
                "Process Email",
                type="primary",
                key="graph_process_email",
                use_container_width=True,
            )

        if process_graph_btn:
            # Clear previous results (both graph and blob tabs)
            st.session_state.graph_shipments = []
            st.session_state.graph_email_processed = False
            st.session_state.graph_email_data = None
            st.session_state.shipments = []
            st.session_state.email_processed = False
            st.session_state.source_breakdown = None
            st.session_state.raw_cu_results = None
            st.session_state.envelope = None
            st.session_state.followup_results = {}
            st.session_state.graph_current_message = None
            st.session_state.graph_is_tracked_reply = False
            st.session_state.graph_correlation_result = None

            selected_message = messages[selected_idx]

            # Store the current MailMessage so the follow-up button can use it
            st.session_state.graph_current_message = selected_message

            # ── Thread-aware correlation (four fallback strategies) ────────
            try:
                _orchestrator = FollowupOrchestrator()
                _correlation  = _orchestrator.correlate_message(selected_message)
                st.session_state.graph_correlation_result = _correlation
                st.session_state.graph_is_tracked_reply   = _correlation.is_reply
            except Exception:
                st.session_state.graph_correlation_result = None
                st.session_state.graph_is_tracked_reply   = False

            with st.container():
                with st.spinner("Downloading email and attachments from Graph API..."):
                    try:
                        reader = GraphEmailReader(client, attachment_output_dir=Config.OUTPUT_DIR)
                        updated_token, email_data = run_async(
                            reader.read_single_email(st.session_state.graph_token, selected_message)
                        )
                        st.session_state.graph_token = updated_token
                    except Exception as e:
                        st.error(f"Failed to download email from Graph API: {e}")
                        logger.error(f"Graph email download error: {e}", exc_info=True)
                        email_data = None

                if email_data:
                    # Show correlation banner when a reply is detected
                    corr: Optional[CorrelationResult] = st.session_state.graph_correlation_result
                    if st.session_state.graph_is_tracked_reply and corr:
                        _method_labels = {
                            "conversation_id": "same conversation thread",
                            "reference_id":    "matching reference / order ID",
                            "subject":         "subject-line similarity",
                            "sender+subject":  "sender address + subject similarity",
                            "sender":          "sender email address",
                        }
                        method_label = _method_labels.get(corr.method, corr.method)
                        st.info(
                            f"Tracked reply detected via **{method_label}** "
                            f"(confidence {corr.confidence:.0%}). "
                            "Reply data will be merged with the existing partial shipment."
                        )

                    # For a tracked reply, extract only — the shipment is created from
                    # the MERGED thread data after handle_reply (Approach A). For a fresh
                    # email, submit normally inside process_graph_email.
                    shipments_data = process_graph_email(
                        email_data=email_data,
                        message_id=selected_message.id or "",
                        submit_shipments=not st.session_state.graph_is_tracked_reply,
                    )

                    # If it's a reply, merge the extracted data with the tracked conversation state
                    if st.session_state.graph_is_tracked_reply and shipments_data:
                        try:
                            _orchestrator = FollowupOrchestrator()
                            reply_token = st.session_state.graph_token
                            reply_client = _init_graph_client()
                            if reply_client and reply_token:
                                first_shipment = shipments_data[0]["shipment"]
                                reply_result = run_async(
                                    _orchestrator.handle_reply(
                                        reply_message=selected_message,
                                        reply_shipment=first_shipment,
                                        graph_client=reply_client,
                                        access_token=reply_token.access_token,
                                        correlation=st.session_state.graph_correlation_result,
                                    )
                                )
                                # Replace the extracted shipment with the merged version
                                if reply_result.shipment:
                                    shipments_data[0]["shipment"] = reply_result.shipment
                                # Store the reply result so the UI can show the appropriate status
                                st.session_state.followup_results[0] = reply_result

                                # ── Approach A: when this reply COMPLETES the thread,
                                # create the shipment from the MERGED conversation data
                                # (all fields gathered across the whole email thread) ──
                                if reply_result.action == "complete" and reply_result.shipment:
                                    _conv = _orchestrator.get_conversation_state(
                                        reply_result.conversation_id
                                    )
                                    _cust = (
                                        _conv.customer_id if _conv and _conv.customer_id is not None
                                        else _get_customer_id(shipments_data[0])
                                    )
                                    st.success(
                                        "Thread complete — all required fields gathered across the "
                                        "conversation. Creating shipment from the merged thread…"
                                    )
                                    _submit_to_brokerware(reply_result.shipment, customer_id=_cust)
                        except Exception as exc:
                            logger.error("Reply merge failed: %s", exc, exc_info=True)
                            st.warning(f"Reply merge encountered an error: {exc}")

                    if shipments_data:
                        st.session_state.graph_shipments = shipments_data
                        st.session_state.graph_email_processed = True
                        # Sync to shared shipments so display_shipment/missing-fields works correctly
                        st.session_state.shipments = shipments_data

                        # ── Auto follow-up: send immediately when fields are missing ──
                        # Only for fresh emails (not customer replies — those go through handle_reply).
                        if not st.session_state.graph_is_tracked_reply:
                            _auto_client = _init_graph_client()
                            _auto_token  = st.session_state.graph_token
                            if _auto_client and _auto_token:
                                for _idx, _sd in enumerate(shipments_data):
                                    _sh = _sd["shipment"]
                                    if (
                                        _sh.email_type in ("shipment_tender", "shipment_quote")
                                        and _has_blocking_missing_fields(_sh)
                                        and _idx not in st.session_state.followup_results
                                    ):
                                        with st.spinner(
                                            "Missing fields detected — auto-sending follow-up email..."
                                        ):
                                            try:
                                                _orch   = FollowupOrchestrator()
                                                _result = run_async(
                                                    _orch.handle_initial_extraction(
                                                        shipment=_sh,
                                                        message=selected_message,
                                                        graph_client=_auto_client,
                                                        access_token=_auto_token.access_token,
                                                        customer_id=_get_customer_id(_sd),
                                                    )
                                                )
                                                st.session_state.followup_results[_idx] = _result
                                            except Exception as _exc:
                                                logger.error(
                                                    "Auto follow-up failed: %s", _exc, exc_info=True
                                                )
                                                st.session_state.followup_results[_idx] = FollowupResult(
                                                    action="error",
                                                    message=str(_exc),
                                                )
                    else:
                        st.error("No shipments were extracted from this email.")

    # ── Display results ───────────────────────────────────────────────────────
    if st.session_state.graph_email_processed and st.session_state.graph_shipments:
        st.divider()

        shipments_list = st.session_state.graph_shipments

        # ── Email summary banner ──────────────────────────────────────────────
        if st.session_state.graph_email_data:
            ed = st.session_state.graph_email_data
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 2, 2])
                c1.markdown(f"**Subject:** {ed.get('subject', '—')}")
                c2.markdown(f"**From:** {ed.get('from', '—')}")
                c3.markdown(f"**Date:** {ed.get('date', '—')[:10] if ed.get('date') else '—'}")

        # ── Conversation lifecycle timeline ───────────────────────────────────
        _render_conversation_timeline()

        # ── Shipment cards ────────────────────────────────────────────────────
        st.subheader(f"Extracted Shipments  ({len(shipments_list)})")

        def _render_shipment_card(shipment_data: dict, shipment_idx: int):
            s: Shipment = shipment_data["shipment"]
            rf = s.required_fields
            nth = s.nice_to_have_fields

            _render_email_type_badge(s.email_type)
            if s.email_type == "spam":
                st.error("Classified as spam — no shipment data extracted.")
                return

            if s.email_type in ("shipment_tender", "shipment_quote") and _has_blocking_missing_fields(s):
                _display_missing_fields_form(shipment_idx)
                st.divider()

            # ── Row 1: Pickup | Delivery ──────────────────────────────────
            col_pick, col_del = st.columns(2)

            with col_pick:
                with st.container(border=True):
                    st.markdown("**Pickup**")
                    if rf.pickup_location:
                        st.markdown(f"**{rf.pickup_location.name or '—'}**")
                        if rf.pickup_location.address:
                            a = rf.pickup_location.address
                            parts = [p for p in [a.street, a.city, a.state, a.zip_code] if p]
                            st.caption(", ".join(parts) if parts else "—")
                    st.markdown(f"**Date:** {rf.pickup_date.strftime('%b %d, %Y') if rf.pickup_date else '—'}")
                    if rf.pickup_window:
                        st.markdown(f"**Window:** {rf.pickup_window}")
                    if nth.pickup_contact:
                        pc = nth.pickup_contact
                        contact_parts = [p for p in [pc.name, pc.phone, pc.email] if p]
                        if contact_parts:
                            st.caption("Contact: " + " · ".join(contact_parts))

            with col_del:
                with st.container(border=True):
                    st.markdown("**Delivery**")
                    if rf.drop_location:
                        st.markdown(f"**{rf.drop_location.name or '—'}**")
                        if rf.drop_location.address:
                            a = rf.drop_location.address
                            parts = [p for p in [a.street, a.city, a.state, a.zip_code] if p]
                            st.caption(", ".join(parts) if parts else "—")
                    st.markdown(f"**Date:** {rf.delivery_date.strftime('%b %d, %Y') if rf.delivery_date else '—'}")
                    if nth.drop_contact:
                        dc = nth.drop_contact
                        contact_parts = [p for p in [dc.name, dc.phone, dc.email] if p]
                        if contact_parts:
                            st.caption("Contact: " + " · ".join(contact_parts))

            # ── Row 2: Equipment | Weight | Customer | Confidence ─────────
            col_eq, col_wt, col_cu, col_conf = st.columns(4)
            col_eq.write(f"**Equipment**\n{rf.equipment_mode or '—'}")
            col_wt.write(f"**Total Weight**\n{f'{rf.total_weight:,.0f} lbs' if rf.total_weight else '—'}")
            col_cu.write(f"**Customer**\n{rf.customer_name or '—'}")
            conf = nth.extraction_confidence
            col_conf.write(f"**Confidence**\n{f'{conf:.0%}' if conf else '—'}")

            # ── Row 3: Items ──────────────────────────────────────────────
            if rf.items:
                with st.expander(f"Freight Items ({len(rf.items)})"):
                    for item in rf.items:
                        parts = []
                        if item.quantity and item.unit:
                            parts.append(f"{item.quantity} {item.unit}")
                        elif item.quantity:
                            parts.append(str(item.quantity))
                        if item.description:
                            parts.append(item.description)
                        if item.weight:
                            parts.append(f"{item.weight} lbs")
                        if item.pieces:
                            parts.append(f"{item.pieces} pcs")
                        if item.pallets:
                            parts.append(f"{item.pallets} pallets")
                        st.markdown("- " + " · ".join(parts) if parts else "- (no detail)")

            # ── Row 4: Nice-to-haves ──────────────────────────────────────
            extras = []
            if nth.reference_numbers:
                extras.append(("Ref #s", ", ".join(nth.reference_numbers)))
            if nth.order_number:
                extras.append(("Order #", nth.order_number))
            if nth.purchase_order:
                extras.append(("PO #", nth.purchase_order))
            if nth.special_instructions:
                extras.append(("Special Instructions", nth.special_instructions))
            if nth.temperature_requirements:
                extras.append(("Temp Requirements", nth.temperature_requirements))
            if rf.accessorials:
                extras.append(("Accessorials", ", ".join(rf.accessorials)))

            if extras:
                with st.expander("Additional Details"):
                    for label, val in extras:
                        st.markdown(f"**{label}:** {val}")

            # ── JSON output ───────────────────────────────────────────────
            with st.expander("View Output JSON"):
                st.code(
                    format_client_json_str(s, customer_id=_get_customer_id(shipment_data)),
                    language="json",
                )

        if len(shipments_list) == 1:
            _render_shipment_card(shipments_list[0], 0)
        else:
            tabs = st.tabs([
                f"Shipment {i+1}: {sd['attachment_name']}"
                for i, sd in enumerate(shipments_list)
            ])
            for tab, (i, sd) in zip(tabs, enumerate(shipments_list)):
                with tab:
                    _render_shipment_card(sd, i)

        # ── Download ──────────────────────────────────────────────────────────
        downloadable = [
            s for s in shipments_list
            if s["shipment"].email_type != "spam"
            and not _has_blocking_missing_fields(s["shipment"])
        ]
        if downloadable:
            import zipfile
            import io

            col_dl, _ = st.columns([1, 2])
            with col_dl:
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    body_shipments = [s for s in downloadable if s.get('is_body_shipment')]
                    non_body_shipments = [s for s in downloadable if not s.get('is_body_shipment')]
                    for idx, sd in enumerate(non_body_shipments, 1):
                        json_content = format_client_json_str(sd["shipment"], customer_id=_get_customer_id(sd))
                        zf.writestr(f"shipment_{idx}_{Path(sd['attachment_name']).stem}.json", json_content)
                    if len(body_shipments) == 1:
                        zf.writestr("shipment_email_body.json",
                            format_client_json_str(body_shipments[0]["shipment"], customer_id=_get_customer_id(body_shipments[0])))
                    elif len(body_shipments) > 1:
                        combined = {
                            f"shipment_{i+1}": json.loads(format_client_json_str(s["shipment"], customer_id=_get_customer_id(s)))
                            for i, s in enumerate(body_shipments)
                        }
                        zf.writestr("shipment_email_body.json", json.dumps(combined, indent=2))
                st.download_button(
                    label="Download All Shipments (ZIP)",
                    data=zip_buffer.getvalue(),
                    file_name="all_shipments.zip",
                    mime="application/zip",
                    type="primary",
                    use_container_width=True,
                    key="graph_dl_all_zip",
                )

        # ── Technical details (collapsed) ─────────────────────────────────────
        with st.expander("Technical Details"):
            tech_tab_labels = []
            if st.session_state.graph_email_data:
                tech_tab_labels.append("Email Preview")
            if st.session_state.raw_cu_results:
                tech_tab_labels.append("Raw OCR JSON")
            if st.session_state.source_breakdown:
                tech_tab_labels.append("Normalized JSON")

            if tech_tab_labels:
                tech_tabs = st.tabs(tech_tab_labels)
                tab_idx = 0
                if st.session_state.graph_email_data:
                    with tech_tabs[tab_idx]:
                        _display_email_preview(st.session_state.graph_email_data)
                    tab_idx += 1
                if st.session_state.raw_cu_results:
                    with tech_tabs[tab_idx]:
                        raw_cu_json = json.dumps(st.session_state.raw_cu_results, indent=2)
                        _render_scrollable_json(raw_cu_json)
                        st.download_button("Download Raw JSON", raw_cu_json,
                            "raw_content_understanding.json", "application/json",
                            key="graph_dl_raw_json")
                    tab_idx += 1
                if st.session_state.source_breakdown:
                    with tech_tabs[tab_idx]:
                        breakdown_json = json.dumps(st.session_state.source_breakdown, indent=2)
                        _render_scrollable_json(breakdown_json)
                        st.download_button("Download Normalized JSON", breakdown_json,
                            "normalized_json.json", "application/json",
                            key="graph_dl_norm_json")


def _render_review_queue_panel():
    """
    Full-page review queue panel.

    Shows all pending ReviewRequests with editable missing-field forms
    and Approve / Reject actions. Approved shipments update the
    ConversationState to 'complete'.
    """
    queue    = ReviewQueue()
    pending  = queue.get_pending()

    st.markdown("## Review Queue")

    if not pending:
        st.success("No pending reviews — all shipments have been actioned.")
        if st.button("Back to Inbox", key="review_back_empty"):
            st.session_state.show_review_queue = False
            st.rerun()
        return

    if st.button("Back to Inbox", key="review_back"):
        st.session_state.show_review_queue = False
        st.rerun()

    st.markdown(f"**{len(pending)} pending review(s)**")
    st.divider()

    for req in pending:
        with st.expander(
            f"🔴 {req.reason_label}  ·  {req.subject[:60] or '(no subject)'}  ·  {req.created_at[:10]}",
            expanded=True,
        ):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**From:** {req.sender_name or ''} `{req.sender_email}`")
                st.markdown(f"**Review ID:** `{req.review_id[:8]}…`")
            with c2:
                st.markdown(f"**Follow-ups sent:** {req.followup_count}")
                if req.extraction_confidence is not None:
                    st.markdown(f"**Extraction confidence:** {req.extraction_confidence:.0%}")

            if req.flags:
                for flag in req.flags:
                    st.warning(flag)

            st.markdown("---")

            # ── Editable missing fields form ──────────────────────────────────
            short_id = req.review_id[:8]
            filled: dict = {}

            if req.missing_fields:
                st.markdown("**Fill missing fields:**")
                for f_key in req.missing_fields:
                    cfg = _REQUIRED_FIELD_CONFIG.get(f_key)
                    if not cfg:
                        filled[f_key] = st.text_input(f_key, key=f"rv_{short_id}_{f_key}")
                        continue
                    label = cfg["label"]
                    ftype = cfg["type"]

                    if ftype == "text":
                        filled[f_key] = st.text_input(f"{label}:", key=f"rv_{short_id}_{f_key}")
                    elif ftype == "date":
                        val = st.date_input(f"{label}:", value=None, key=f"rv_{short_id}_{f_key}")
                        filled[f_key] = val.isoformat() if val else None
                    elif ftype == "address":
                        st.markdown(f"**{label}:**")
                        a1, a2 = st.columns(2)
                        with a1:
                            street = st.text_input("Street:", key=f"rv_{short_id}_{f_key}_street")
                            city   = st.text_input("City:",   key=f"rv_{short_id}_{f_key}_city")
                        with a2:
                            state  = st.text_input("State:",  key=f"rv_{short_id}_{f_key}_state")
                            zip_c  = st.text_input("Zip:",    key=f"rv_{short_id}_{f_key}_zip")
                        filled[f_key] = {"street": street, "city": city, "state": state, "zip": zip_c}
                    else:
                        filled[f_key] = st.text_input(f"{label}:", key=f"rv_{short_id}_{f_key}")

                st.markdown("---")
            else:
                st.info("No specific missing fields recorded — reviewer can approve as-is.")

            # ── Extracted data reference ───────────────────────────────────────
            with st.expander("View extracted shipment data (read-only)"):
                import json as _json
                st.code(_json.dumps(req.shipment_data, indent=2, default=str), language="json")

            # ── Reviewer notes ────────────────────────────────────────────────
            notes = st.text_area(
                "Reviewer notes (optional):",
                key=f"rv_notes_{short_id}",
                height=60,
            )

            # ── Action buttons ────────────────────────────────────────────────
            btn_col1, btn_col2, btn_col3 = st.columns([2, 2, 2])

            with btn_col1:
                approve_clicked = st.button(
                    "Approve & Complete",
                    key=f"rv_approve_{short_id}",
                    type="primary",
                    use_container_width=True,
                )

            with btn_col3:
                reject_clicked = st.button(
                    "Reject",
                    key=f"rv_reject_{short_id}",
                    use_container_width=True,
                )

            # ── Handle approval ───────────────────────────────────────────────
            if approve_clicked:
                # Merge reviewer-supplied field values into the stored shipment dict
                approved = dict(req.shipment_data)
                rf = approved.get("requiredFields", {})
                for f_key, val in filled.items():
                    if not val:
                        continue
                    if f_key in ("pickupDate", "deliveryDate"):
                        rf[f_key] = val
                    elif f_key in ("pickupLocation", "dropLocation"):
                        loc = rf.get(f_key) or {}
                        addr = loc.get("address") or {}
                        addr.update({
                            "street":  val.get("street") or addr.get("street"),
                            "city":    val.get("city")   or addr.get("city"),
                            "state":   val.get("state")  or addr.get("state"),
                            "zipCode": val.get("zip")    or addr.get("zipCode"),
                        })
                        loc["address"] = addr
                        rf[f_key] = loc
                    elif f_key == "customerName":
                        rf["customerName"] = val
                    elif f_key == "equipmentMode":
                        rf["equipmentMode"] = val
                    elif f_key == "totalWeight":
                        try:
                            rf["totalWeight"] = float(val)
                        except (ValueError, TypeError):
                            pass
                    else:
                        rf[f_key] = val
                approved["requiredFields"] = rf

                queue.approve(req.review_id, approved, notes)

                # Update the ConversationState to 'complete'
                if req.conversation_id:
                    try:
                        from src.services.conversation_tracker import ConversationTracker
                        from src.models.conversation_state import EVENT_CONVERSATION_COMPLETE
                        tracker = ConversationTracker()
                        state   = tracker.load(req.conversation_id)
                        if state:
                            state.status = "complete"
                            state.add_event(
                                EVENT_CONVERSATION_COMPLETE,
                                {"source": "hitl_review", "review_id": req.review_id},
                            )
                            tracker.save(state)
                    except Exception as exc:
                        logger.warning("Could not update ConversationState after HITL approval: %s", exc)

                st.success(f"Review {short_id}… approved. Shipment marked as complete.")
                st.rerun()

            # ── Handle rejection ──────────────────────────────────────────────
            if reject_clicked:
                queue.reject(req.review_id, notes)
                st.error(f"Review {short_id}… rejected.")
                st.rerun()


def main():
    """Main Streamlit app"""
    
    # Header with logo and title in a single flexbox container for precise spacing control
    assets_dir = Path(__file__).parent / "assets"
    logo_path = None
    
    # Check for animated formats first
    for ext in ['.gif', '.mp4', '.webm']:
        potential_logo = assets_dir / f"3pl_logo{ext}"
        if potential_logo.exists():
            logo_path = potential_logo
            break
    
    # Fallback to static PNG
    if not logo_path:
        logo_path = assets_dir / "3pl_logo.png"
    
    # Build logo HTML
    logo_html = ""
    if logo_path.exists():
        import base64
        if logo_path.suffix.lower() == '.gif':
            with open(logo_path, "rb") as f:
                gif_bytes = f.read()
            gif_base64 = base64.b64encode(gif_bytes).decode()
            logo_html = f'<img src="data:image/gif;base64,{gif_base64}" width="200" style="display: block;" />'
        elif logo_path.suffix.lower() in ['.mp4', '.webm']:
            with open(logo_path, "rb") as f:
                video_bytes = f.read()
            video_base64 = base64.b64encode(video_bytes).decode()
            video_ext = logo_path.suffix.lower()
            video_mime = "video/mp4" if video_ext == ".mp4" else "video/webm"
            logo_html = f'<video width="200" autoplay loop muted playsinline style="display: block;"><source src="data:{video_mime};base64,{video_base64}" type="{video_mime}"></video>'
        else:
            # For static images, we'll use a different approach
            with open(logo_path, "rb") as f:
                img_bytes = f.read()
            img_base64 = base64.b64encode(img_bytes).decode()
            img_ext = logo_path.suffix.lower()
            img_mime = f"image/{img_ext[1:]}" if img_ext else "image/png"
            logo_html = f'<img src="data:{img_mime};base64,{img_base64}" width="200" style="display: block;" />'
    
    # Create single flexbox container with logo and title side by side with minimal gap
    header_html = f"""
    <div style="display: flex; align-items: center; gap: 15px; padding: 10px 0;">
        <div style="flex-shrink: 0;">
            {logo_html if logo_html else '<h3>Shepherd AI</h3>'}
        </div>
        <div style="flex-grow: 1;">
            <h1 style="margin: 0; padding: 0; font-size: 2.5rem;">Shepherd AI - Shipment Extractor</h1>
        </div>
    </div>
    """
    st.markdown(header_html, unsafe_allow_html=True)
    st.divider()

    # ── Review Queue panel (full-page view when toggled) ───────────────────────
    if st.session_state.get("show_review_queue"):
        _render_review_queue_panel()
        return

    # ── Review Queue banner (shown when pending reviews exist) ─────────────────
    try:
        _rq     = ReviewQueue()
        _pending = _rq.get_pending()
        if _pending:
            col_msg, col_btn = st.columns([6, 2])
            with col_msg:
                st.warning(
                    f"**{len(_pending)} shipment(s) pending human review** — "
                    "max follow-ups reached or send error."
                )
            with col_btn:
                if st.button("Open Review Queue", key="open_rq_banner", type="primary", use_container_width=True):
                    st.session_state.show_review_queue = True
                    st.rerun()
            st.divider()
    except Exception:
        pass

    # ── Live Mailbox ───────────────────────────────────────────────────────────
    _render_graph_tab()



def _format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _render_scrollable_json(json_str: str, height: int = 400) -> None:
    """Render a JSON string in an iframe with always-visible scrollbars (both axes)."""
    safe = json_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    pre_height = height - 4
    html = (
        "<style>"
        "body{margin:0;padding:0;background:#f8f9fa;}"
        f"pre{{margin:0;padding:12px;height:{pre_height}px;overflow:scroll;"
        "background:#f8f9fa;color:#1a1a1a;"
        "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;font-size: 14px;"
        "white-space:pre;box-sizing:border-box;"
        "border:1px solid #d0d0d0;border-radius:6px;}"
        "pre::-webkit-scrollbar{width:12px;height:12px;}"
        "pre::-webkit-scrollbar-track{background:#e8e8e8;border-radius:4px;}"
        "pre::-webkit-scrollbar-thumb{background:#888;border-radius:4px;}"
        "pre::-webkit-scrollbar-thumb:hover{background:#555;}"
        "pre::-webkit-scrollbar-corner{background:#e8e8e8;}"
        "</style>"
        f"<pre>{safe}</pre>"
    )
    st_components.html(html, height=height, scrolling=False)


def _display_email_preview(email_data: dict):
    """Render email header, body (as rendered HTML), and downloadable attachments."""

    subject     = email_data.get('subject', '(No subject)')
    sender      = email_data.get('from', '')
    to          = email_data.get('to', '')
    date        = email_data.get('date', '')
    html_body   = email_data.get('html_body', '')
    plain_body  = email_data.get('body', '')
    attachments = email_data.get('attachments', [])

    # ── Header card ──────────────────────────────────────────────────────────
    st.markdown(
        f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:16px 20px 12px;background:#fafafa;margin-bottom:12px;">
            <div style="font-size:1.1rem;font-weight:700;margin-bottom:10px;color:#111;">{subject}</div>
            <table style="border-collapse:collapse;font-size:0.875rem;color:#444;">
                <tr>
                    <td style="padding:3px 16px 3px 0;font-weight:600;color:#555;white-space:nowrap;">From</td>
                    <td style="padding:3px 0;">{sender}</td>
                </tr>
                <tr>
                    <td style="padding:3px 16px 3px 0;font-weight:600;color:#555;white-space:nowrap;">To</td>
                    <td style="padding:3px 0;">{to}</td>
                </tr>
                <tr>
                    <td style="padding:3px 16px 3px 0;font-weight:600;color:#555;white-space:nowrap;">Date</td>
                    <td style="padding:3px 0;">{date}</td>
                </tr>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Email body ────────────────────────────────────────────────────────────
    if html_body:
        # Wrap in a minimal shell so fonts and box model behave correctly
        wrapped = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
    font-size: 14px;
    color: #222;
    margin: 16px;
    line-height: 1.5;
  }}
</style>
</head>
<body>{html_body}</body>
</html>"""
        st.iframe(wrapped, height=500)
    elif plain_body:
        st.text(plain_body)

    # ── Attachments ───────────────────────────────────────────────────────────
    _downloadable_exts = {'.pdf', '.xlsx', '.xls'}
    attachments = [a for a in attachments if Path(a.get('filepath', a.get('filename', ''))).suffix.lower() in _downloadable_exts]

    if attachments:
        st.markdown(f"**Attachments ({len(attachments)})**")
        for att in attachments:
            filepath = Path(att.get('filepath', ''))
            filename = att.get('filename', filepath.name)
            size_str = _format_file_size(att.get('size', 0))

            col_name, col_size, col_btn = st.columns([5, 1, 1])
            with col_name:
                st.markdown(f" &nbsp;{filename}", unsafe_allow_html=True)
            with col_size:
                st.markdown(
                    f"<div style='color:#888;font-size:0.85rem;padding-top:6px;'>{size_str}</div>",
                    unsafe_allow_html=True,
                )
            with col_btn:
                if filepath.exists():
                    with open(filepath, 'rb') as f:
                        file_bytes = f.read()
                    st.download_button(
                        label="Download",
                        data=file_bytes,
                        file_name=filename,
                        mime=att.get('content_type', 'application/octet-stream'),
                        key=f"preview_dl_{filename}",
                        use_container_width=True,
                    )
                else:
                    st.markdown(
                        "<div style='color:#aaa;font-size:0.85rem;padding-top:6px;'>Unavailable</div>",
                        unsafe_allow_html=True,
                    )


# Badge config for each email classification type
_EMAIL_TYPE_BADGE = {
    "shipment_tender":  ("#1a6b3a", "#e8f5ee", "Shipment – Tender"),
    "shipment_quote":   ("#1a4a8a", "#eef3ff", "Shipment – Quote"),
    "tracking_request": ("#0a4f8c", "#e8f0fb", "Tracking Request"),
    "status_update":    ("#6b4a00", "#fff8e1", "Status Update"),
    "spam":             ("#a01e1e", "#fbeaea", "Spam / Irrelevant"),
    "other":            ("#b85c00", "#fdf3e7", "Other"),
}

# Human-readable labels and input types for each required field
_REQUIRED_FIELD_CONFIG = {
    "customerName":   {"label": "Customer Name",       "type": "text"},
    "pickupLocation": {"label": "Pickup Location",     "type": "address"},
    "dropLocation":   {"label": "Delivery Location",   "type": "address"},
    "shipperZip":     {"label": "Pickup Zip Code",     "type": "zip"},
    "consigneeZip":   {"label": "Delivery Zip Code",   "type": "zip"},
    "pickupDate":     {"label": "Pickup Date",         "type": "date"},
    "deliveryDate":   {"label": "Delivery Date",       "type": "date"},
    "equipmentMode":  {"label": "Equipment / Mode",    "type": "text"},
    "items":          {"label": "Commodities",         "type": "warning_only"},
}


def _render_email_type_badge(email_type: str):
    color, bg, label = _EMAIL_TYPE_BADGE.get(email_type, ("#b85c00", "#fdf3e7", "Other"))
    st.markdown(
        f'<div style="display:inline-flex;align-items:center;gap:8px;background-color:{bg};'
        f'border:1.5px solid {color};border-radius:20px;padding:7px 18px;">'
        f'<span style="width:9px;height:9px;border-radius:50%;background:{color};flex-shrink:0;"></span>'
        f'<span style="color:{color} !important;font-size:0.95rem;font-weight:700;">{label}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown("")


def _has_blocking_missing_fields(shipment: Shipment) -> bool:
    """Returns True only if there are missing required fields that need user input (not warning_only)."""
    return any(
        _REQUIRED_FIELD_CONFIG.get(f, {}).get("type") != "warning_only"
        for f in shipment.missing_required_fields
    )


def _apply_missing_fields(shipment_idx: int, field_values: dict):
    """Merge human-provided values into the shipment and remove resolved fields."""
    shipment = st.session_state.shipments[shipment_idx]["shipment"]
    rf = shipment.required_fields
    resolved = []

    for field, value in field_values.items():
        if field == "customerName" and value:
            rf.customer_name = value
            resolved.append("customerName")

        elif field == "pickupLocation":
            if any(v for v in value.values() if v):
                if not rf.pickup_location:
                    rf.pickup_location = LocationInfo()
                if not rf.pickup_location.address:
                    rf.pickup_location.address = ShipmentAddress()
                addr = rf.pickup_location.address
                if value.get("street"): addr.street = value["street"]
                if value.get("city"):   addr.city   = value["city"]
                if value.get("state"):  addr.state  = value["state"]
                if value.get("zip"):    addr.zip_code = value["zip"]
                resolved.append("pickupLocation")

        elif field == "dropLocation":
            if any(v for v in value.values() if v):
                if not rf.drop_location:
                    rf.drop_location = LocationInfo()
                if not rf.drop_location.address:
                    rf.drop_location.address = ShipmentAddress()
                addr = rf.drop_location.address
                if value.get("street"): addr.street = value["street"]
                if value.get("city"):   addr.city   = value["city"]
                if value.get("state"):  addr.state  = value["state"]
                if value.get("zip"):    addr.zip_code = value["zip"]
                resolved.append("dropLocation")

        elif field == "shipperZip" and value:
            if not rf.pickup_location:
                rf.pickup_location = LocationInfo()
            if not rf.pickup_location.address:
                rf.pickup_location.address = ShipmentAddress()
            rf.pickup_location.address.zip_code = value
            resolved.append("shipperZip")

        elif field == "consigneeZip" and value:
            if not rf.drop_location:
                rf.drop_location = LocationInfo()
            if not rf.drop_location.address:
                rf.drop_location.address = ShipmentAddress()
            rf.drop_location.address.zip_code = value
            resolved.append("consigneeZip")

        elif field == "pickupDate" and value:
            from datetime import datetime as dt
            rf.pickup_date = dt.combine(value, dt.min.time())
            resolved.append("pickupDate")

        elif field == "deliveryDate" and value:
            from datetime import datetime as dt
            rf.delivery_date = dt.combine(value, dt.min.time())
            resolved.append("deliveryDate")

        elif field == "equipmentMode" and value:
            rf.equipment_mode = value
            resolved.append("equipmentMode")

    # Auto-resolve warning_only fields — they have no input widget, so clear them on submit
    warning_only_resolved = [
        f for f in shipment.missing_required_fields
        if _REQUIRED_FIELD_CONFIG.get(f, {}).get("type") == "warning_only"
    ]
    resolved.extend(warning_only_resolved)

    shipment.missing_required_fields = [
        f for f in shipment.missing_required_fields if f not in resolved
    ]
    st.session_state.shipments[shipment_idx]["shipment"] = shipment

    # Once all blocking required fields are filled, save to blob and submit to Brokerware
    if not _has_blocking_missing_fields(shipment):
        shipment_data = st.session_state.shipments[shipment_idx]
        cid = _get_customer_id(shipment_data)
        _save_shipment_json_to_blob(
            shipment,
            email_name=shipment_data.get("email_name", "unknown"),
            source_name=shipment_data["attachment_name"],
            index=shipment_data["attachment_index"],
            customer_id=cid,
        )
        _submit_to_brokerware(shipment, customer_id=cid)


def _display_missing_fields_form(shipment_idx: int):
    """Render a labeled input form for each missing required field."""
    shipment = st.session_state.shipments[shipment_idx]["shipment"]
    missing  = shipment.missing_required_fields

    # ── Automated follow-up email section ─────────────────────────────────
    followup_result: Optional[FollowupResult] = st.session_state.followup_results.get(shipment_idx)

    if followup_result and followup_result.action == "followup_sent":
        st.success(
            f"Follow-up email #{followup_result.followup_count} sent successfully. "
            "Waiting for customer reply. The conversation is being tracked automatically."
        )
        with st.expander("View sent email preview"):
            st.markdown(followup_result.followup_html or "", unsafe_allow_html=True)
        st.divider()

    elif followup_result and followup_result.action == "complete":
        st.success("All required fields are now present after the customer's reply. Shipment is ready for creation.")
        st.divider()

    elif followup_result and followup_result.action == "max_retries":
        already_routed = str(shipment_idx) in st.session_state.review_routed or shipment_idx in st.session_state.review_routed
        if already_routed:
            rid = st.session_state.review_routed.get(shipment_idx) or st.session_state.review_routed.get(str(shipment_idx), "")
            st.warning(
                f"Routed to Review Queue (ID: `{str(rid)[:8]}…`). "
                "Open the **Review Queue** panel to action this shipment."
            )
        else:
            st.error(
                f"Maximum follow-ups reached ({followup_result.followup_count}). "
                "This shipment requires human review."
            )
            current_message: Optional[MailMessage] = st.session_state.get("graph_current_message")
            if current_message:
                if st.button("Route to Review Queue", key=f"hitl_max_{shipment_idx}", type="primary"):
                    router = HITLRouter()
                    decision = router.evaluate(shipment=shipment, followup_result=followup_result)
                    conv_id  = followup_result.conversation_id or (current_message.conversation_id or "")
                    review   = router.route(
                        shipment=shipment,
                        message=current_message,
                        decision=decision,
                        conversation_id=conv_id,
                        followup_count=followup_result.followup_count,
                    )
                    st.session_state.review_routed[shipment_idx] = review.review_id
                    st.session_state.show_review_queue = True
                    st.rerun()
        st.divider()

    elif followup_result and followup_result.action == "error":
        already_routed = str(shipment_idx) in st.session_state.review_routed or shipment_idx in st.session_state.review_routed
        if already_routed:
            rid = st.session_state.review_routed.get(shipment_idx) or st.session_state.review_routed.get(str(shipment_idx), "")
            st.warning(f"Routed to Review Queue (ID: `{str(rid)[:8]}…`). Open the **Review Queue** panel to action this shipment.")
        else:
            st.error(f"Follow-up error: {followup_result.message}")
            current_message_err: Optional[MailMessage] = st.session_state.get("graph_current_message")
            if current_message_err and missing:
                if st.button("Route to Review Queue", key=f"hitl_err_{shipment_idx}", type="primary"):
                    router   = HITLRouter()
                    decision = router.evaluate(shipment=shipment, followup_result=followup_result)
                    conv_id  = current_message_err.conversation_id or ""
                    review   = router.route(
                        shipment=shipment,
                        message=current_message_err,
                        decision=decision,
                        conversation_id=conv_id,
                    )
                    st.session_state.review_routed[shipment_idx] = review.review_id
                    st.session_state.show_review_queue = True
                    st.rerun()
        st.divider()

    # Show automated send button only when connected to a live mailbox
    graph_connected = st.session_state.get("graph_connected", False)
    current_message: Optional[MailMessage] = st.session_state.get("graph_current_message")
    graph_token = st.session_state.get("graph_token")

    if graph_connected and current_message and graph_token:
        already_sent = followup_result and followup_result.action in ("followup_sent", "complete")
        if not already_sent:
            blocking = [f for f in missing if f not in ("items",)]
            if blocking:
                col_btn, col_info = st.columns([2, 5])
                with col_btn:
                    send_clicked = st.button(
                        "Send Follow-Up Email",
                        key=f"send_followup_{shipment_idx}",
                        type="primary",
                        use_container_width=True,
                    )
                with col_info:
                    st.caption(
                        f"Will request {len(blocking)} missing field(s) from the customer "
                        "as a reply in the same email thread."
                    )

                if send_clicked:
                    client = _init_graph_client()
                    if client:
                        with st.spinner("Generating and sending follow-up email..."):
                            try:
                                orchestrator = FollowupOrchestrator()
                                _sd = st.session_state.shipments[shipment_idx]
                                result = run_async(
                                    orchestrator.handle_initial_extraction(
                                        shipment=shipment,
                                        message=current_message,
                                        graph_client=client,
                                        access_token=graph_token.access_token,
                                        customer_id=_get_customer_id(_sd),
                                    )
                                )
                                st.session_state.followup_results[shipment_idx] = result
                            except Exception as exc:
                                logger.error("Follow-up send failed: %s", exc, exc_info=True)
                                st.session_state.followup_results[shipment_idx] = FollowupResult(
                                    action="error",
                                    message=str(exc),
                                )
                        st.rerun()
        st.divider()
    else:
        st.warning(
            "⚠️ This shipment is missing required fields. "
            "Connect to a live mailbox (Live Mailbox tab) to enable automated follow-up emails, "
            "or manually reply to the customer thread with the requested details."
        )

    # Hide the manual form while waiting for a customer reply — it becomes
    # relevant again only if the automated follow-up failed or was never sent.
    followup_pending = followup_result and followup_result.action in ("followup_sent", "complete")
    if followup_pending:
        return

    field_values = {}

    with st.form(key=f"missing_fields_{shipment_idx}"):
        for field in missing:
            cfg = _REQUIRED_FIELD_CONFIG.get(field)
            if not cfg:
                continue

            label = cfg["label"]
            ftype = cfg["type"]

            if ftype == "text":
                field_values[field] = st.text_input(f"{label}:")

            elif ftype == "address":
                st.markdown(f"**{label}:**")
                c1, c2 = st.columns(2)
                with c1:
                    street = st.text_input("Street Address:", key=f"{field}_street_{shipment_idx}")
                    city   = st.text_input("City:",           key=f"{field}_city_{shipment_idx}")
                with c2:
                    state  = st.text_input("State:",    key=f"{field}_state_{shipment_idx}")
                    zip_c  = st.text_input("Zip Code:", key=f"{field}_zip_{shipment_idx}")
                field_values[field] = {"street": street, "city": city, "state": state, "zip": zip_c}

            elif ftype == "zip":
                field_values[field] = st.text_input(f"{label}:", key=f"{field}_{shipment_idx}", max_chars=10)

            elif ftype == "date":
                field_values[field] = st.date_input(f"{label}:", value=None, key=f"{field}_{shipment_idx}")

        submitted = st.form_submit_button("Submit Missing Fields", type="primary")
        if submitted:
            _apply_missing_fields(shipment_idx, field_values)
            st.rerun()


def display_shipment(shipment: Shipment, attachment_name: str, index: int, shipment_idx: int = 0, customer_id: Optional[int] = None):
    """Render a single shipment's extracted data."""
    _render_email_type_badge(shipment.email_type)

    # Spam — nothing to display
    if shipment.email_type == "spam":
        st.error("This email has been classified as spam or irrelevant. No shipment data was extracted.")
        return

    # Missing fields form (shipment creation only)
    if shipment.email_type in ("shipment_tender", "shipment_quote") and _has_blocking_missing_fields(shipment):
        _display_missing_fields_form(shipment_idx)
        st.divider()

    rf = shipment.required_fields

    st.subheader("JSON Data")
    with st.expander("View JSON"):
        st.code(format_client_json_str(shipment, customer_id=customer_id), language="json")


if __name__ == "__main__":
    main()
