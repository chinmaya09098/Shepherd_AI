"""
Microsoft Graph API change notification (webhook) models.

Matches the JSON schema of notifications POSTed by Microsoft Graph to the
webhook_handler Azure Function when a subscribed mailbox receives a new email.

Ref: https://learn.microsoft.com/en-us/graph/api/resources/changenotification
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ResourceData(BaseModel):
    """Minimal resource data embedded in a change notification."""
    odata_type: Optional[str] = Field(None, alias="@odata.type")
    odata_id: Optional[str] = Field(None, alias="@odata.id")
    odata_etag: Optional[str] = Field(None, alias="@odata.etag")
    id: Optional[str] = None    # Graph message ID

    model_config = {"populate_by_name": True}


class ChangeNotification(BaseModel):
    """A single change notification from Microsoft Graph."""
    id: Optional[str] = None
    subscription_id: Optional[str] = Field(None, alias="subscriptionId")
    subscription_expiration_date_time: Optional[str] = Field(
        None, alias="subscriptionExpirationDateTime"
    )
    change_type: str = Field("created", alias="changeType")
    client_state: Optional[str] = Field(None, alias="clientState")
    resource: Optional[str] = None
    resource_data: Optional[ResourceData] = Field(None, alias="resourceData")
    tenant_id: Optional[str] = Field(None, alias="tenantId")

    model_config = {"populate_by_name": True}

    def get_message_id(self) -> Optional[str]:
        """Extract the Graph message ID from resourceData."""
        if self.resource_data and self.resource_data.id:
            return self.resource_data.id
        # Fall back to parsing from resource URL: me/messages/{id}
        if self.resource:
            parts = self.resource.rstrip("/").split("/")
            if parts:
                return parts[-1]
        return None


class ChangeNotificationCollection(BaseModel):
    """Batch of change notifications (Graph API POST payload)."""
    value: List[ChangeNotification] = Field(default_factory=list)
    validation_tokens: Optional[List[str]] = Field(None, alias="validationTokens")

    model_config = {"populate_by_name": True}


class WebhookSubscription(BaseModel):
    """
    Metadata for a Graph API webhook subscription.

    Azure Table Storage schema:
      Table:         shepherdsubscriptions
      PartitionKey:  tenant_id
      RowKey:        subscription_id
    """
    subscription_id: str
    tenant_id: str = "default"
    mailbox_upn: str = ""
    resource: str = "me/mailFolders/inbox/messages"
    change_types: List[str] = Field(default_factory=lambda: ["created"])
    notification_url: str = ""
    client_state: str = ""              # Secret token for validating incoming notifications
    expiration_date_time: Optional[datetime] = None
    created_at: Optional[datetime] = None
    is_active: bool = True

    model_config = {"arbitrary_types_allowed": True}


class QueueMessage(BaseModel):
    """
    Message written to Azure Queue Storage by webhook_handler
    and consumed by email_processor.
    """
    message_id: str
    conversation_id: str = ""
    tenant_id: str = "default"
    subscription_id: str = ""
    received_at: str = ""               # ISO timestamp when notification was received
    retry_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)
