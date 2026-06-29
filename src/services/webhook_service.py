"""
Microsoft Graph webhook subscription management.

Graph subscriptions for mail resources expire after a maximum of 4230 minutes
(~3 days). This service creates, renews, and deletes subscriptions and
persists metadata in Azure Table Storage so the renewal timer knows what to renew.

Renewal is triggered by the subscription_renewal Azure Function (timer trigger).

Ref: https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

from src.models.webhook_models import WebhookSubscription
from src.utils.logger import get_logger

logger = get_logger(__name__)

_GRAPH_URL = "https://graph.microsoft.com/v1.0"
_LIFETIME_MINUTES = 4200          # Just under the 4230-minute Graph API maximum
_TABLE_NAME = "shepherdsubscriptions"


class WebhookService:
    """Creates and manages Graph API webhook subscriptions."""

    def __init__(self, connection_string: Optional[str] = None):
        self._table = None
        if connection_string:
            try:
                from azure.data.tables import TableServiceClient
                svc = TableServiceClient.from_connection_string(connection_string)
                self._table = svc.create_table_if_not_exists(_TABLE_NAME)
                logger.info("WebhookService ready (table: %s)", _TABLE_NAME)
            except ImportError:
                logger.warning("azure-data-tables not installed")
            except Exception as exc:
                logger.error("WebhookService init failed: %s", exc)

    # ── Subscription lifecycle ─────────────────────────────────────────────────

    async def create_subscription(
        self,
        access_token: str,
        notification_url: str,
        resource: str = "me/mailFolders/inbox/messages",
        tenant_id: str = "default",
    ) -> Optional[WebhookSubscription]:
        """
        Create a new Graph change notification subscription.

        Args:
            access_token:     Valid delegated Graph access token
            notification_url: Publicly reachable HTTPS URL (webhook_handler function URL)
            resource:         Graph resource path to monitor
            tenant_id:        Tenant identifier for storage

        Returns:
            WebhookSubscription on success, None on failure
        """
        client_state = secrets.token_hex(16)
        expiry = datetime.now(timezone.utc) + timedelta(minutes=_LIFETIME_MINUTES)

        payload = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "clientState": client_state,
        }

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    f"{_GRAPH_URL}/subscriptions",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    content=json.dumps(payload),
                )

            if resp.status_code != 201:
                logger.error(
                    "Subscription creation failed: %s — %s",
                    resp.status_code,
                    resp.text[:500],
                )
                return None

            data = resp.json()
            sub = WebhookSubscription(
                subscription_id=data["id"],
                tenant_id=tenant_id,
                resource=resource,
                notification_url=notification_url,
                client_state=client_state,
                expiration_date_time=datetime.fromisoformat(
                    data["expirationDateTime"].replace("Z", "+00:00")
                ),
                created_at=datetime.now(timezone.utc),
                is_active=True,
            )

            self._save(sub)
            logger.info("Subscription created: %s", sub.subscription_id)
            return sub

        except Exception as exc:
            logger.error("create_subscription error: %s", exc)
            return None

    async def renew_subscription(
        self,
        subscription_id: str,
        access_token: str,
    ) -> bool:
        """
        Extend an expiring subscription by another _LIFETIME_MINUTES.
        Called by the subscription_renewal timer trigger.
        """
        new_expiry = datetime.now(timezone.utc) + timedelta(minutes=_LIFETIME_MINUTES)
        payload = {"expirationDateTime": new_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.patch(
                    f"{_GRAPH_URL}/subscriptions/{subscription_id}",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    content=json.dumps(payload),
                )

            if resp.status_code == 200:
                logger.info("Subscription %s renewed → %s", subscription_id, new_expiry)
                return True

            logger.error(
                "Subscription renewal failed: %s — %s",
                resp.status_code,
                resp.text[:500],
            )
            return False

        except Exception as exc:
            logger.error("renew_subscription error for %s: %s", subscription_id, exc)
            return False

    async def delete_subscription(
        self,
        subscription_id: str,
        access_token: str,
    ) -> bool:
        """Remove a subscription from Graph API."""
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.delete(
                    f"{_GRAPH_URL}/subscriptions/{subscription_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            if resp.status_code in (200, 204):
                logger.info("Subscription %s deleted", subscription_id)
                return True
            return False
        except Exception as exc:
            logger.error("delete_subscription error: %s", exc)
            return False

    # ── Table helpers ─────────────────────────────────────────────────────────

    def get_all_active(self, tenant_id: str = "default") -> List[WebhookSubscription]:
        """Return all active subscriptions for a tenant (used by renewal timer)."""
        if not self._table:
            return []
        try:
            flt = f"PartitionKey eq '{tenant_id}' and is_active eq true"
            return [_entity_to_sub(e) for e in self._table.query_entities(flt)]
        except Exception as exc:
            logger.error("get_all_active failed: %s", exc)
            return []

    def deactivate(self, subscription_id: str, tenant_id: str = "default") -> None:
        """Mark a subscription as inactive in the table."""
        if not self._table:
            return
        try:
            entity = self._table.get_entity(
                partition_key=tenant_id,
                row_key=subscription_id,
            )
            entity["is_active"] = False
            self._table.update_entity(entity)
        except Exception as exc:
            logger.error("deactivate failed for %s: %s", subscription_id, exc)

    def _save(self, sub: WebhookSubscription) -> None:
        if not self._table:
            return
        try:
            entity = {
                "PartitionKey": sub.tenant_id,
                "RowKey": sub.subscription_id,
                "subscription_id": sub.subscription_id,
                "resource": sub.resource,
                "notification_url": sub.notification_url,
                "client_state": sub.client_state,
                "is_active": sub.is_active,
                "expiration_date_time": (
                    sub.expiration_date_time.isoformat()
                    if sub.expiration_date_time else ""
                ),
                "created_at": (
                    sub.created_at.isoformat() if sub.created_at else ""
                ),
            }
            self._table.upsert_entity(entity)
        except Exception as exc:
            logger.error("WebhookService._save failed: %s", exc)


def _entity_to_sub(entity: dict) -> WebhookSubscription:
    return WebhookSubscription(
        subscription_id=entity.get("RowKey", ""),
        tenant_id=entity.get("PartitionKey", "default"),
        resource=entity.get("resource", ""),
        notification_url=entity.get("notification_url", ""),
        client_state=entity.get("client_state", ""),
        is_active=bool(entity.get("is_active", True)),
        expiration_date_time=(
            datetime.fromisoformat(entity["expiration_date_time"])
            if entity.get("expiration_date_time") else None
        ),
    )
