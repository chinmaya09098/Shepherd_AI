"""
Write helpers for the email_records table.

`record_email()` accepts an EMLParser/Graph-compatible email dict and stores a
single row. All failures are logged and swallowed — persistence must never break
the extraction pipeline.
"""
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional, Iterable

from src.db.database import init_db, get_session
from src.db.models import EmailRecord
from src.utils.logger import get_logger

logger = get_logger(__name__)


# Content labels the LLM assigns to shipment-bearing emails.
_SHIPMENT_TYPES = {"shipment_tender", "shipment_quote"}


def derive_class(direction: str, llm_email_types: Optional[Iterable[str]] = None) -> str:
    """
    Map an email to the coarse "Class" code stored in the DB.

      - Outbound (Shepherd's own replies)        -> 'AI'
      - Inbound with shipment content            -> 'SM'  (Shipment)
      - Inbound, anything else                   -> 'CM'  (Customer Message)
    """
    if direction == "Outbound":
        return "AI"
    types = set(llm_email_types or [])
    if types & _SHIPMENT_TYPES:
        return "SM"
    return "CM"


def _parse_received(email_data: dict) -> Optional[datetime]:
    """Best-effort extraction of the received timestamp from an email dict."""
    received = email_data.get("received_date_time")  # Graph: already a datetime
    if isinstance(received, datetime):
        return received
    raw_date = email_data.get("date")  # .eml: RFC-2822 string
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date)
        except Exception:
            return None
    return None


def store_email_record(**kwargs) -> Optional[str]:
    """Low-level insert. Returns the new row id as a string, or None on failure."""
    if not init_db():
        logger.debug("DB not configured — skipping email record insert")
        return None
    try:
        with get_session() as session:
            record = EmailRecord(**kwargs)
            session.add(record)
            session.commit()
            record_id = str(record.id)
        logger.info(
            "Stored email record %s (class=%s, type=%s)",
            record_id, kwargs.get("class_code"), kwargs.get("email_type"),
        )
        return record_id
    except Exception as e:
        logger.error(f"Failed to store email record: {e}")
        return None


def record_email(
    email_data: dict,
    *,
    direction: str = "Inbound",
    client_id: Optional[object] = None,
    llm_email_types: Optional[Iterable[str]] = None,
    extra_metadata: Optional[dict] = None,
) -> Optional[str]:
    """
    Build and persist an EmailRecord from an email dict.

    Args:
        email_data:      EMLParser/GraphEmailReader-compatible dict.
        direction:       'Inbound' (received) or 'Outbound' (Shepherd reply).
        client_id:       resolved Hyperion customerId (stored as text).
        llm_email_types: per-attachment email types from the extractor; used to
                         derive the 'SM' vs 'CM' class for inbound mail.
        extra_metadata:  optional extra context merged into the metadata column.

    Returns:
        The new row id (str), or None if the DB is unconfigured / insert failed.
    """
    sender_name, sender_email = parseaddr(email_data.get("from", "") or "")
    attachments = email_data.get("attachments", []) or []

    metadata = {
        "to": email_data.get("to"),
        "internet_message_id": email_data.get("internet_message_id"),
        "has_attachments": bool(attachments),
        "attachment_filenames": [a.get("filename") for a in attachments],
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    return store_email_record(
        client_id=str(client_id) if client_id is not None else None,
        conversation_id=email_data.get("conversation_id") or None,
        message_id=(
            email_data.get("message_id")
            or email_data.get("internet_message_id")
            or None
        ),
        class_code=derive_class(direction, llm_email_types),
        sender_email=sender_email or None,
        sender_name=sender_name or None,
        mail_subject=email_data.get("subject") or None,
        email_type=direction,
        attachment_count=len(attachments),
        email_metadata=metadata,
        received_at=_parse_received(email_data),
    )
