"""
Graph Email Reader.
Reads emails from Microsoft Graph API and converts them to the same dict
format that EMLParser.parse() returns, so the rest of the Shepherd pipeline
(ContentUnderstandingExtractor, OpenAIAgent) works unchanged.

This replaces EMLParser as the email source when emails come from live
Outlook mailboxes via Graph API instead of .eml files on disk.

Output format — identical to EMLParser.parse():
{
    'subject':          str,
    'from':             str,   # "Name <email>" or "email"
    'to':               str,   # semicolon-separated recipients
    'date':             str,   # ISO format receivedDateTime
    'body':             str,   # plain text body
    'html_body':        str,   # HTML body (may be empty)
    'attachments':      [
        {
            'filename':     str,
            'filepath':     str,
            'content_type': str,
            'size':         int,
        }
    ],
    # Extra Graph-specific fields for SoW conversation/thread management:
    'message_id':           str,   # Graph message ID
    'conversation_id':      str,   # Outlook conversation ID (thread key)
    'internet_message_id':  str,   # RFC 2822 Message-ID header
    'is_read':              bool,
    'has_attachments':      bool,
    'received_date_time':   datetime | None,
}
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.models.graph_models import GraphToken, MailMessage
from src.services.graph_client import GraphClient
from src.utils.logger import get_logger

logger = get_logger(__name__)


class GraphEmailReader:
    """
    Reads emails from Microsoft Graph API and converts them to EMLParser-compatible dicts.

    Usage:
        config  = GraphConfig(tenant_id=..., client_id=..., client_secret=..., redirect_uri=...)
        client  = GraphClient(config)
        reader  = GraphEmailReader(client, attachment_output_dir="output")

        token, email_dicts = await reader.read_inbox_emails(token)
        # email_dicts is a list of dicts identical in shape to EMLParser.parse() output.
        # Pass each dict directly into ShepherdAIProcessor.process_email_dict() (or equivalent).
    """

    # Image extensions that are never shipping documents (mirrors main.py _IMAGE_EXTENSIONS)
    _IMAGE_EXTENSIONS = {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".ico"
    }

    def __init__(
        self,
        graph_client: GraphClient,
        attachment_output_dir: str = "output",
    ):
        self._client = graph_client
        self._output_dir = Path(attachment_output_dir)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def read_inbox_emails(
        self,
        token: GraphToken,
        latest_message_time: Optional[datetime] = None,
    ) -> Tuple[GraphToken, List[Dict]]:
        """
        Read new emails from the Inbox and convert to EMLParser-compatible dicts.

        Args:
            token: Current GraphToken (refreshed internally if near expiry)
            latest_message_time: Only return emails received after this datetime.
                                 Pass the receivedDateTime of the last processed email
                                 to implement incremental sync (avoids reprocessing).

        Returns:
            (updated_token, list_of_email_dicts)
            updated_token must be persisted by the caller if tokens were refreshed.
        """
        token, messages = await self._client.read_inbox(token, latest_message_time)

        email_dicts = []
        for message in messages:
            email_dict = await self._convert_message(token.access_token, message)
            if email_dict:
                email_dicts.append(email_dict)

        logger.info(
            "GraphEmailReader: converted %d messages to email dicts", len(email_dicts)
        )
        return token, email_dicts

    async def read_single_email(
        self,
        token: GraphToken,
        message: MailMessage,
    ) -> Tuple[GraphToken, Optional[Dict]]:
        """
        Convert a single MailMessage (e.g. received via webhook) to an email dict.

        Use this when the webhook notification delivers one message at a time,
        rather than polling the full inbox.

        Args:
            token: Current GraphToken
            message: MailMessage object from a webhook notification

        Returns:
            (updated_token, email_dict) or (token, None) on failure
        """
        token = await self._client.validate_token(token)
        email_dict = await self._convert_message(token.access_token, message)
        return token, email_dict

    # -------------------------------------------------------------------------
    # Internal conversion
    # -------------------------------------------------------------------------

    async def _convert_message(
        self,
        access_token: str,
        message: MailMessage,
    ) -> Optional[Dict]:
        """
        Convert a MailMessage + its attachments into an EMLParser-compatible dict.
        """
        try:
            from_str = self._format_recipient(message.from_)
            to_str = "; ".join(
                self._format_recipient(r) for r in (message.to_recipients or [])
                if r and r.email_address
            )
            date_str = (
                message.received_date_time.isoformat()
                if message.received_date_time
                else ""
            )

            plain_body, html_body = self._extract_body(message)

            # Download attachments if the message has any
            attachments = []
            if message.has_attachments and message.id:
                att_dir = str(
                    self._output_dir
                    / "attachments"
                    / (message.conversation_id or message.id or "unknown")
                )
                raw_attachments = await self._client.download_attachments(
                    access_token, message.id, att_dir
                )
                # Filter out inline images (logos, signatures) — mirrors main.py logic
                attachments = [
                    a for a in raw_attachments
                    if Path(a["filename"]).suffix.lower() not in self._IMAGE_EXTENSIONS
                ]
                filtered = len(raw_attachments) - len(attachments)
                if filtered:
                    logger.debug(
                        "Filtered %d inline image attachment(s) from message %s",
                        filtered,
                        message.id,
                    )

            return {
                # EMLParser-compatible fields
                "subject": message.subject or "",
                "from": from_str,
                "to": to_str,
                "date": date_str,
                "body": plain_body,
                "html_body": html_body,
                "attachments": attachments,
                # Graph-specific fields required by SoW thread + conversation management
                "message_id": message.id or "",
                "conversation_id": message.conversation_id or "",
                "internet_message_id": message.internet_message_id or "",
                "is_read": message.is_read,
                "has_attachments": message.has_attachments,
                "received_date_time": message.received_date_time,
            }

        except Exception as exc:
            logger.error(
                "Error converting message %s to email dict: %s", message.id, exc
            )
            return None

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    @staticmethod
    def _format_recipient(recipient) -> str:
        """
        Format an EmailRecipient as 'Name <email>' or just 'email'.
        Mirrors EMLParser's handling of From/To fields.
        """
        if not recipient or not recipient.email_address:
            return ""
        ea = recipient.email_address
        if ea.name and ea.address:
            return f"{ea.name} <{ea.address}>"
        return ea.address or ea.name or ""

    @staticmethod
    def _extract_body(message: MailMessage):
        """
        Extract plain text and HTML body from a MailMessage.
        Mirrors EMLParser._extract_body() and _extract_html_body().

        Returns:
            (plain_text, html_text)
        """
        if not message.body:
            return "", ""

        content = message.body.content or ""
        content_type = (message.body.content_type or "").lower()

        if content_type == "html":
            html_body = content
            plain_body = GraphEmailReader._strip_html(html_body)
            return plain_body, html_body

        # Plain text — no HTML available
        return content, ""

    @staticmethod
    def _strip_html(html: str) -> str:
        """
        Strip HTML tags to produce a plain text fallback.
        Used when only HTML body is available from Graph API.
        """
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
