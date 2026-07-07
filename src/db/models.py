"""SQLAlchemy 2.0 models for Shepherd AI persistence."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Text, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


class EmailRecord(Base):
    """
    One row per email handled by the system (inbound load tenders / customer
    messages, and outbound Shepherd replies).

    Field notes:
      - id              : UUID primary key (generated app-side).
      - client_id       : the resolved Hyperion customerId for the email, as text
                          (nullable — not every email resolves to a client).
      - conversation_id : Graph/Outlook thread key that groups related messages.
                          Empty for .eml files (no thread context).
      - message_id      : unique per-message id (Graph message id, falls back to
                          the RFC-2822 Internet-Message-Id).
      - class_code      : coarse classification — 'SM' (Shipment), 'CM' (Customer
                          Message), 'AI' (Shepherd's reply). This is the "Class" field.
      - email_type      : direction of the email — 'Inbound' or 'Outbound'.
                          (Distinct from the LLM's shipment_tender/quote/... labels,
                          which describe content, not direction.)
      - email_metadata  : JSONB blob of email context (to/cc, attachment names, etc.).
                          Stored in a column literally named `metadata`.
      - received_at     : when the email was received (inbound) or sent (outbound).
      - created_at      : when this row was written (DB clock).
    """
    __tablename__ = "email_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(512), index=True, nullable=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(512), index=True, nullable=True)

    # "Class" field — column named class_code to avoid SQL reserved-word friction.
    class_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    sender_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    sender_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    mail_subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 'Inbound' or 'Outbound'
    email_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    attachment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Python attr `email_metadata` -> DB column `metadata` (Base.metadata is reserved).
    email_metadata: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)

    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<EmailRecord id={self.id} class={self.class_code} "
            f"type={self.email_type} subject={self.mail_subject!r}>"
        )
