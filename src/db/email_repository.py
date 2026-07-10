"""
Write helpers for the email_records table.

`record_email()` accepts an EMLParser/Graph-compatible email dict and stores a
single row. All failures are logged and swallowed — persistence must never break
the extraction pipeline.
"""
from __future__ import annotations

from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Optional, Iterable

from sqlalchemy import select

from src.db.database import init_db, get_session
from src.db.models import EmailRecord
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.models.graph_models import MailMessage

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


def upsert_inbox_email(msg: "MailMessage") -> Optional[str]:
    """
    Phase 1 — called by Streamlit immediately after the inbox is loaded.

    Inserts a lightweight EmailRecord for a MailMessage with class_code=NULL
    (unprocessed). If a row for the same message_id already exists the call
    is a no-op and the existing row id is returned.

    class_code is intentionally left NULL here and will be filled in later
    by update_email_class() once the processing pipeline completes.

    Returns the row id (str) or None on failure / DB not configured.
    """
    if not init_db():
        logger.debug("DB not configured — skipping inbox email upsert")
        return None

    message_id = msg.id or msg.internet_message_id
    if not message_id:
        return None

    try:
        with get_session() as session:
            # Skip if already stored (idempotent)
            existing_id = session.execute(
                select(EmailRecord.id)
                .where(EmailRecord.message_id == message_id)
                .limit(1)
            ).scalar_one_or_none()
            if existing_id is not None:
                return str(existing_id)

            sender_name_val = ""
            sender_email_val = ""
            if msg.from_ and msg.from_.email_address:
                ea = msg.from_.email_address
                sender_name_val = ea.name or ""
                sender_email_val = ea.address or ""

            to_list = [
                r.email_address.address
                for r in (msg.to_recipients or [])
                if r.email_address and r.email_address.address
            ]

            record = EmailRecord(
                message_id=message_id,
                conversation_id=msg.conversation_id or None,
                sender_email=sender_email_val or None,
                sender_name=sender_name_val or None,
                mail_subject=msg.subject or None,
                email_type="Inbound",
                attachment_count=1 if msg.has_attachments else 0,
                class_code=None,           # filled after processing
                status="pending",          # will be updated to 'processed'/'failed'
                has_missing_fields=False,  # updated to True if follow-up is sent
                email_metadata={
                    "to": to_list,
                    "internet_message_id": msg.internet_message_id,
                    "has_attachments": msg.has_attachments,
                    "pipeline_stage": "inbox_seen",
                },
                received_at=msg.received_date_time,
            )
            session.add(record)
            session.commit()
            row_id = str(record.id)

        logger.info("Inbox email stored (unprocessed) id=%s message_id=%s", row_id, message_id)
        return row_id

    except Exception as e:
        logger.error("Failed to upsert inbox email: %s", e)
        return None


def update_email_class(
    message_id: str,
    class_code: str,
    *,
    client_id: Optional[object] = None,
    has_missing_fields: bool = False,
    failed: bool = False,
    extra_metadata: Optional[dict] = None,
) -> bool:
    """
    Phase 2 — called by the processing pipeline (function_app or Streamlit)
    after extraction + classification is complete.

    Finds the existing EmailRecord by message_id and patches:
      - class_code        : SM / CM / AI
      - status            : 'processed' (default) or 'failed'
      - has_missing_fields: True when a follow-up was sent for missing fields
      - client_id         : resolved Hyperion customerId (if known)
      - email_metadata    : merged with extra_metadata dict

    Returns True if the row was found and updated, False otherwise.
    When False the caller should fall back to record_email() to insert a fresh row.
    """
    if not init_db():
        logger.debug("DB not configured — skipping email class update")
        return False

    try:
        with get_session() as session:
            record = session.execute(
                select(EmailRecord)
                .where(EmailRecord.message_id == message_id)
                .limit(1)
            ).scalar_one_or_none()

            if record is None:
                logger.debug("No record found for message_id=%s — update skipped", message_id)
                return False

            record.class_code = class_code
            record.status = "failed" if failed else "processed"
            record.has_missing_fields = has_missing_fields
            if client_id is not None:
                record.client_id = str(client_id)
            if extra_metadata:
                merged = dict(record.email_metadata or {})
                merged.update(extra_metadata)
                record.email_metadata = merged

            session.commit()

        logger.info(
            "Updated email record class_code=%s client_id=%s for message_id=%s",
            class_code, client_id, message_id,
        )
        return True

    except Exception as e:
        logger.error("Failed to update email class: %s", e)
        return False
