"""
Microsoft Graph API data models.
Python conversion of all models defined in GraphTest/Services/Service.cs.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class GraphConfig:
    """
    Azure AD app registration credentials.
    Equivalent to AzureModel in Service.cs.
    """
    tenant_id: str
    client_id: str
    client_secret: str
    redirect_uri: str = "https://localhost:5001/signin-oidc"


@dataclass
class GraphToken:
    """
    OAuth token state for a single tenant mailbox.
    Equivalent to GraphModel in Service.cs.
    """
    access_token: str
    refresh_token: str
    expiration: datetime


@dataclass
class MailFolder:
    """
    A single mail folder (Inbox, Sent, etc.).
    Equivalent to OAPIFolder in Service.cs.
    """
    id: Optional[str] = None
    display_name: Optional[str] = None
    parent_folder_id: Optional[str] = None
    child_folder_count: int = 0
    unread_item_count: int = 0
    total_item_count: int = 0
    size_in_bytes: int = 0
    is_hidden: bool = False


@dataclass
class MailBody:
    """
    Email body content (plain text or HTML).
    Equivalent to OAPIBody in Service.cs.
    """
    content_type: Optional[str] = None
    content: Optional[str] = None


@dataclass
class EmailAddress:
    """
    An email address with optional display name.
    Equivalent to OAPIEmailAddress in Service.cs.
    """
    name: Optional[str] = None
    address: Optional[str] = None


@dataclass
class EmailRecipient:
    """
    A recipient wrapper around EmailAddress.
    Equivalent to OAPIEmail in Service.cs.
    """
    email_address: Optional[EmailAddress] = None


@dataclass
class MailFlag:
    """
    Mail flag status.
    Equivalent to OAPIFlag in Service.cs.
    """
    flag_status: Optional[str] = None


@dataclass
class MailMessage:
    """
    A full email message from Microsoft Graph API.
    Equivalent to OAPIValue in Service.cs.
    Includes conversation_id and internet_message_id required by SoW thread management.
    """
    id: Optional[str] = None
    internet_message_id: Optional[str] = None
    conversation_id: Optional[str] = None
    created_date_time: Optional[datetime] = None
    received_date_time: Optional[datetime] = None
    sent_date_time: Optional[datetime] = None
    subject: Optional[str] = None
    body_preview: Optional[str] = None
    importance: Optional[str] = None
    parent_folder_id: Optional[str] = None
    conversation_index: Optional[str] = None
    is_read: bool = False
    is_draft: bool = False
    web_link: Optional[str] = None
    has_attachments: bool = False
    body: Optional[MailBody] = None
    sender: Optional[EmailRecipient] = None
    from_: Optional[EmailRecipient] = None
    to_recipients: List[EmailRecipient] = field(default_factory=list)
    cc_recipients: List[EmailRecipient] = field(default_factory=list)
    flag: Optional[MailFlag] = None


@dataclass
class ReadInboxRequest:
    """
    Request payload for reading inbox (passed in from caller).
    Equivalent to ReadInboxRequest in Service.cs.
    """
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expiration: Optional[datetime] = None


@dataclass
class InboxMessageSummary:
    """
    Lightweight summary of one inbox message.
    Equivalent to InboxMessageSummary in Service.cs.
    """
    subject: Optional[str] = None
    from_address: Optional[str] = None
    received_date_time: Optional[datetime] = None


@dataclass
class ReadInboxResult:
    """
    Result returned after reading the inbox.
    Equivalent to ReadInboxResult in Service.cs.
    """
    message_count: int = 0
    messages: List[InboxMessageSummary] = field(default_factory=list)


@dataclass
class UserProfile:
    """
    Microsoft Graph user profile (/me endpoint).
    Equivalent to the private UserProfile class in OutlookController.cs.
    """
    id: str = ""
    user_principal_name: str = ""
    mail: str = ""  # Primary SMTP address — may differ from UPN for AD accounts
