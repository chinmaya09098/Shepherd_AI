"""
Streamlit UI for Shepherd AI POC - Email Shipment Extraction
"""
import streamlit as st
import tempfile
import json
import logging
import asyncio
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
from src.services.hyperion_client import resolve_customer_id
from src.services.search_client import find_customer_matches
from src.utils.blob_log_handler import BlobLogHandler
from src.utils.logger import get_logger
from src.ui.styles import get_light_theme_css
from src.services.graph_client import GraphClient
from src.models.graph_models import GraphConfig, GraphToken, MailMessage
from src.readers.graph_email_reader import GraphEmailReader

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
if 'graph_mail' not in st.session_state:
    st.session_state.graph_mail = None  # Real SMTP address (may differ from UPN)
if 'graph_messages' not in st.session_state:
    st.session_state.graph_messages = []
if 'graph_auto_load' not in st.session_state:
    st.session_state.graph_auto_load = False
if 'graph_shipments' not in st.session_state:
    st.session_state.graph_shipments = []
if 'graph_email_processed' not in st.session_state:
    st.session_state.graph_email_processed = False
if 'graph_email_data' not in st.session_state:
    st.session_state.graph_email_data = None


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

    Only runs when the parent location object exists — if the location
    itself is absent it is already flagged at the location level.
    """
    rf = shipment.required_fields

    if (rf.pickup_location and
            not (rf.pickup_location.address and rf.pickup_location.address.zip_code)):
        if "shipperZip" not in shipment.missing_required_fields:
            shipment.missing_required_fields.append("shipperZip")

    if (rf.drop_location and
            not (rf.drop_location.address and rf.drop_location.address.zip_code)):
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
                            customer_id=resolve_customer_id(email_data.get('from', '')),
                        )
                    st.success(f"Extracted shipment data from {attachment['filename']}")
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
                        customer_id=resolve_customer_id(email_data.get('from', '')),
                    )
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
                    st.success("Extracted shipment data from email body")
                else:
                    st.warning("Could not extract shipment data from email body.")
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
                    st.success(f"Extracted shipment data from {attachment['filename']}")
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
                st.success("Extracted shipment data from email body")
            else:
                st.warning("Could not extract shipment data from email body. The email may not contain shipment information.")

        Path(tmp_path).unlink(missing_ok=True)

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


def _save_tokens_to_env(token: GraphToken) -> None:
    """Persist Graph tokens to .env so they survive Streamlit restarts."""
    from dotenv import set_key
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        set_key(str(env_path), "GRAPH_ACCESS_TOKEN", token.access_token or "")
        set_key(str(env_path), "GRAPH_REFRESH_TOKEN", token.refresh_token or "")
        expiry = token.expiration.isoformat() if token.expiration else ""
        set_key(str(env_path), "GRAPH_TOKEN_EXPIRATION", expiry)
    except Exception as e:
        logger.warning("Could not save tokens to .env: %s", e)


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


def process_graph_email(email_data: dict, message_id: str) -> List[dict]:
    """
    Process a Graph API email dict through the full extraction pipeline.

    Equivalent to process_email_blob() but takes an already-converted email_data
    dict from GraphEmailReader instead of downloading from Azure Blob Storage.

    Args:
        email_data: EMLParser-compatible dict from GraphEmailReader._convert_message()
        message_id: Graph message ID (used for naming saved outputs)

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
        st.success(f"Email loaded: {email_data.get('subject', '(No subject)')}")
        st.info(f"Found {len(email_data.get('attachments', []))} attachments")

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
            # Pre-extract text from all attachments
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

            # Per-source structured extraction
            with st.spinner("Extracting structured data per source..."):
                breakdown = {}
                breakdown["emailBody"] = openai_agent.extract_source_fields(email_data['body'])
                for d in attachment_data:
                    breakdown[d['attachment']['filename']] = openai_agent.extract_source_fields(d['text'])
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

            # Pass 2: per-attachment extraction
            progress_bar = st.progress(0)
            status_text = st.empty()

            for idx, data in enumerate(attachment_data, 1):
                attachment = data['attachment']
                status_text.markdown(
                    f'<p style="color: #FFFFFF !important; font-size: 16px; font-weight: 500;">'
                    f'Processing attachment {idx}/{len(attachment_data)}: {attachment["filename"]}</p>',
                    unsafe_allow_html=True,
                )
                progress_bar.progress(idx / len(attachment_data))

                with st.spinner(f"Extracting shipment data from {attachment['filename']}..."):
                    shipment = openai_agent.extract_shipment_data(
                        attachment_text=data['text'],
                        envelope=envelope
                    )

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
                    st.success(f"Extracted shipment data from {attachment['filename']}")
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
                st.success("Extracted shipment data from email body")
            else:
                st.warning("Could not extract shipment data from email body. The email may not contain shipment information.")

        return shipments

    except Exception as e:
        st.error(f"Error processing Graph email: {str(e)}")
        logger.error(f"Error processing Graph email: {e}", exc_info=True)
        return []


def _render_graph_tab():
    """Render the Live Mailbox (Microsoft Graph) tab."""

    # ── Step 1: Handle OAuth callback (?code= redirect from Microsoft) ────────
    params = st.query_params
    auth_code = params.get("code")
    auth_state = params.get("state")

    if auth_code and auth_state in ("add", "update") and not st.session_state.graph_connected:
        _client = _init_graph_client()
        if _client:
            with st.spinner("Completing Microsoft sign-in..."):
                try:
                    token = run_async(_client.exchange_code_for_tokens(auth_code))
                    user_profile = run_async(_client.get_user_profile(token.access_token))
                    st.session_state.graph_token = token
                    st.session_state.graph_connected = True
                    st.session_state.graph_user = (
                        user_profile.user_principal_name if user_profile else "Unknown"
                    )
                    st.session_state.graph_mail = (
                        user_profile.mail if user_profile and user_profile.mail else None
                    )
                    st.query_params.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Microsoft sign-in failed: {e}")
                    logger.error("Graph OAuth callback error: %s", e, exc_info=True)
                    st.query_params.clear()
                    return

    # ── Step 2: Check credentials are configured ───────────────────────────────
    graph_configured = all([
        Config.AZURE_AD_TENANT_ID,
        Config.AZURE_AD_CLIENT_ID,
        Config.AZURE_AD_CLIENT_SECRET,
    ])

    if not graph_configured:
        st.warning(
            "Microsoft Graph API credentials are not configured. "
            "Add **AZURE_AD_TENANT_ID**, **AZURE_AD_CLIENT_ID**, and **AZURE_AD_CLIENT_SECRET** "
            "to your `.env` file."
        )
        return

    client = _init_graph_client()

    # ── Step 3: Not connected — show Sign In button ────────────────────────────
    if not st.session_state.graph_connected:
        st.markdown("<br><br>", unsafe_allow_html=True)
        _, col_c, _ = st.columns([1, 2, 1])
        with col_c:
            st.markdown("### Connect your mailbox")
            st.markdown(
                "Sign in with your Microsoft account to read emails "
                "directly from your inbox."
            )
            st.markdown("<br>", unsafe_allow_html=True)
            auth_url = client.get_auth_url(state="add")
            st.markdown(
                f'<a href="{auth_url}" target="_self" style="display:block;'
                f'text-align:center;padding:12px 24px;background-color:#0078d4;'
                f'color:white;text-decoration:none;border-radius:6px;'
                f'font-weight:600;font-size:1rem;">'
                f'Sign in with Microsoft</a>',
                unsafe_allow_html=True,
            )
        return

    # ── Step 4: Connected — status bar + disconnect ────────────────────────────
    col_status, col_disconnect = st.columns([4, 1])
    with col_status:
        user_label = st.session_state.graph_user or "Connected"
        smtp = st.session_state.graph_mail
        label = f"Connected as: **{user_label}**"
        if smtp and smtp != user_label:
            label += f"  ·  Mailbox: **{smtp}**"
        st.success(label)
    with col_disconnect:
        if st.button("Disconnect", key="graph_disconnect"):
            st.session_state.graph_token = None
            st.session_state.graph_connected = False
            st.session_state.graph_user = None
            st.session_state.graph_mail = None
            st.session_state.graph_messages = []
            st.session_state.graph_shipments = []
            st.session_state.graph_email_processed = False
            st.session_state.graph_email_data = None
            st.rerun()

    # ── Step 5: Auto-load inbox once after sign-in ────────────────────────────
    if not st.session_state.graph_messages:
        with st.spinner("Loading inbox..."):
            try:
                updated_token, messages = run_async(
                    client.read_inbox(st.session_state.graph_token)
                )
                st.session_state.graph_token = updated_token
                st.session_state.graph_messages = messages
            except Exception as e:
                st.error(f"Failed to load inbox: {e}")
                logger.error("Graph inbox load error: %s", e, exc_info=True)
                return

    # ── Step 6: Email list and processing ────────────────────────────────────
    messages: List[MailMessage] = st.session_state.graph_messages

    if not messages:
        st.info("Inbox is empty. No emails found.")
        return

    st.success(f"Loaded {len(messages)} email(s) from inbox")

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
        st.session_state.graph_shipments = []
        st.session_state.graph_email_processed = False
        st.session_state.graph_email_data = None
        st.session_state.shipments = []
        st.session_state.email_processed = False
        st.session_state.source_breakdown = None
        st.session_state.raw_cu_results = None
        st.session_state.envelope = None

        selected_message = messages[selected_idx]

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
            shipments_data = process_graph_email(
                email_data=email_data,
                message_id=selected_message.id or "",
            )
            if shipments_data:
                st.session_state.graph_shipments = shipments_data
                st.session_state.graph_email_processed = True
                st.session_state.shipments = shipments_data
            else:
                st.error("No shipments were extracted from this email.")

    # ── Display results ───────────────────────────────────────────────────────
    if st.session_state.graph_email_processed and st.session_state.graph_shipments:
        st.divider()

        if st.session_state.raw_cu_results:
            raw_cu_json = json.dumps(st.session_state.raw_cu_results, indent=2)
            with st.expander("View Raw JSON"):
                st.code(raw_cu_json, language="json")
            st.download_button(
                label="Download Raw JSON",
                data=raw_cu_json,
                file_name="raw_content_understanding.json",
                mime="application/json",
                type="secondary",
                key="graph_dl_raw_json",
            )

        if st.session_state.graph_email_data:
            with st.expander("Email Preview"):
                _display_email_preview(st.session_state.graph_email_data)

        if st.session_state.source_breakdown:
            breakdown_json = json.dumps(st.session_state.source_breakdown, indent=2)
            with st.expander("Normalized JSON"):
                st.code(breakdown_json, language="json")
            st.download_button(
                label="Download Normalized JSON",
                data=breakdown_json,
                file_name="normalized_json.json",
                mime="application/json",
                type="secondary",
                key="graph_dl_norm_json",
            )

        st.divider()
        st.header("Extracted Shipments")

        shipments_list = st.session_state.graph_shipments
        st.info(f"Found {len(shipments_list)} shipment(s)")

        if len(shipments_list) == 1:
            shipment_data = shipments_list[0]
            display_shipment(
                shipment_data["shipment"],
                shipment_data["attachment_name"],
                1,
                shipment_idx=0,
                customer_id=_get_customer_id(shipment_data),
            )
        else:
            result_tabs = st.tabs([
                f"Shipment {idx}: {data['attachment_name']}"
                for idx, data in enumerate(shipments_list, 1)
            ])
            for result_tab, (idx, shipment_data) in zip(result_tabs, enumerate(shipments_list)):
                with result_tab:
                    display_shipment(
                        shipment_data["shipment"],
                        shipment_data["attachment_name"],
                        shipment_data["attachment_index"],
                        shipment_idx=idx,
                        customer_id=_get_customer_id(shipment_data),
                    )

        # Download ZIP of complete shipments
        downloadable = [
            s for s in shipments_list
            if s["shipment"].email_type != "spam"
            and not _has_blocking_missing_fields(s["shipment"])
        ]
        if downloadable:
            import zipfile
            import io

            st.divider()
            col1, _ = st.columns([1, 1])
            with col1:
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    body_shipments = [s for s in downloadable if s.get('is_body_shipment')]
                    non_body_shipments = [s for s in downloadable if not s.get('is_body_shipment')]

                    for idx, sd in enumerate(non_body_shipments, 1):
                        json_content = format_client_json_str(sd["shipment"], customer_id=_get_customer_id(sd))
                        filename = f"shipment_{idx}_{Path(sd['attachment_name']).stem}.json"
                        zf.writestr(filename, json_content)

                    if len(body_shipments) == 1:
                        zf.writestr(
                            "shipment_email_body.json",
                            format_client_json_str(body_shipments[0]["shipment"], customer_id=_get_customer_id(body_shipments[0])),
                        )
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
                    type="secondary",
                    use_container_width=True,
                    key="graph_dl_all_zip",
                )


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

    # ── Main tabs ─────────────────────────────────────────────────────────────
    tab_graph, tab_blob = st.tabs(["Live Mailbox (Microsoft Graph)", "Azure Blob Storage"])

    with tab_graph:
        _render_graph_tab()

    with tab_blob:
        # Load emails from Azure Blob Storage
        selected_blob: Optional[str] = None

        with st.spinner("Loading emails from Azure Blob Storage..."):
            st.session_state.blob_emails = list_email_blobs(prefix="sample-mails/")

        if not st.session_state.blob_emails:
            st.warning("No .eml files found in Azure Blob Storage under 'sample-mails/'.")
        else:
            selected_blob = st.selectbox(
                "Select an email from Azure Blob Storage to extract shipment information from attachments",
                options=st.session_state.blob_emails,
                format_func=lambda name: Path(name).name,
            )

        # Process button (only when a blob is selected)
        if selected_blob is not None:
            col1, col2 = st.columns([1, 4])
            with col1:
                process_button = st.button("Process Email", type="primary", use_container_width=True, key="process_email")

            if process_button:
                # Clear previous results (both tabs)
                st.session_state.shipments = []
                st.session_state.email_processed = False
                st.session_state.email_data = None
                st.session_state.envelope = None
                st.session_state.source_breakdown = None
                st.session_state.raw_cu_results = None
                st.session_state.graph_shipments = []
                st.session_state.graph_email_processed = False
                st.session_state.graph_email_data = None

                # Process email from Azure Blob Storage
                with st.container():
                    shipments_data = process_email_blob(selected_blob)

                    if shipments_data:
                        st.session_state.shipments = shipments_data
                        st.session_state.email_processed = True
                        st.success(f"Successfully processed {len(shipments_data)} shipments!")
                    else:
                        st.error("No shipments were extracted. Please check the email file and try again.")

        # Display results
        if st.session_state.email_processed and st.session_state.shipments:
            st.divider()

            # Raw Content Understanding JSON (before email preview)
            if st.session_state.raw_cu_results:
                raw_cu_json = json.dumps(st.session_state.raw_cu_results, indent=2)
                with st.expander("View Raw JSON"):
                    st.code(raw_cu_json, language="json")
                st.download_button(
                    label="Download Raw JSON",
                    data=raw_cu_json,
                    file_name="raw_content_understanding.json",
                    mime="application/json",
                    type="secondary",
                )

            # Email preview
            if st.session_state.email_data:
                with st.expander("Email Preview"):
                    _display_email_preview(st.session_state.email_data)

            # Normalized JSON (per-source structured breakdown)
            if st.session_state.source_breakdown:
                breakdown_json = json.dumps(st.session_state.source_breakdown, indent=2)
                with st.expander("Normalized JSON"):
                    st.code(breakdown_json, language="json")
                st.download_button(
                    label="Download Normalized JSON",
                    data=breakdown_json,
                    file_name="normalized_json.json",
                    mime="application/json",
                    type="secondary",
                )

            st.divider()

            st.header("Extracted Shipments")
            st.info(f"Found {len(st.session_state.shipments)} shipment(s)")

            if len(st.session_state.shipments) == 1:
                shipment_data = st.session_state.shipments[0]
                display_shipment(
                    shipment_data["shipment"],
                    shipment_data["attachment_name"],
                    1,
                    shipment_idx=0,
                    customer_id=_get_customer_id(shipment_data),
                )
            else:
                tabs = st.tabs([
                    f"Shipment {idx}: {data['attachment_name']}"
                    for idx, data in enumerate(st.session_state.shipments, 1)
                ])
                for tab, (idx, shipment_data) in zip(tabs, enumerate(st.session_state.shipments)):
                    with tab:
                        display_shipment(
                            shipment_data["shipment"],
                            shipment_data["attachment_name"],
                            shipment_data["attachment_index"],
                            shipment_idx=idx,
                            customer_id=_get_customer_id(shipment_data),
                        )

            # Download button — exclude spam
            downloadable = [
                s for s in st.session_state.shipments
                if s["shipment"].email_type != "spam"
                and not _has_blocking_missing_fields(s["shipment"])
            ]
            if downloadable:
                import zipfile
                import io

                st.divider()
                col1, col2 = st.columns([1, 1])
                with col1:
                    zip_buffer = io.BytesIO()
                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                        body_shipments = [s for s in downloadable if s.get('is_body_shipment')]
                        non_body_shipments = [s for s in downloadable if not s.get('is_body_shipment')]

                        for idx, shipment_data in enumerate(non_body_shipments, 1):
                            json_content = format_client_json_str(shipment_data["shipment"], customer_id=_get_customer_id(shipment_data))
                            filename = f"shipment_{idx}_{Path(shipment_data['attachment_name']).stem}.json"
                            zf.writestr(filename, json_content)

                        if len(body_shipments) == 1:
                            zf.writestr("shipment_email_body.json", format_client_json_str(body_shipments[0]["shipment"], customer_id=_get_customer_id(body_shipments[0])))
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
                        type="secondary",
                        use_container_width=True,
                    )



def _format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


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

    # Once all blocking required fields are filled, save to blob
    if not _has_blocking_missing_fields(shipment):
        shipment_data = st.session_state.shipments[shipment_idx]
        _save_shipment_json_to_blob(
            shipment,
            email_name=shipment_data.get("email_name", "unknown"),
            source_name=shipment_data["attachment_name"],
            index=shipment_data["attachment_index"],
            customer_id=_get_customer_id(shipment_data),
        )


def _display_missing_fields_form(shipment_idx: int):
    """Render a labeled input form for each missing required field."""
    shipment = st.session_state.shipments[shipment_idx]["shipment"]
    missing  = shipment.missing_required_fields

    st.warning("⚠️ This shipment is missing required fields. A customer follow-up is required - please reply to the existing email thread and request the customer to respond with the missing details in the same thread.")

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

            # elif ftype == "warning_only":
            #     st.warning(
            #         f"**{label} could not be extracted automatically.** "
            #         "Please verify the source document contains item/commodity details and reprocess."
            #     )

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
    if shipment.email_type == "shipment_tender" and _has_blocking_missing_fields(shipment):
        _display_missing_fields_form(shipment_idx)
        st.divider()

    rf = shipment.required_fields

    # JSON view — only shown once all blocking required fields are present
    if not (shipment.email_type == "shipment_tender" and _has_blocking_missing_fields(shipment)):
        st.subheader("JSON Data")
        with st.expander("View JSON"):
            st.code(format_client_json_str(shipment, customer_id=customer_id), language="json")


if __name__ == "__main__":
    main()
