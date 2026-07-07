"""
Microsoft Graph API webhook subscription manager.

Handles creation, renewal, and deletion of Graph API change-notification
subscriptions so the Function App receives push notifications for new emails.

Subscription lifecycle:
  1. register()    — create a new subscription (max 4320 min / 3 days for Mail.Read)
  2. renew()       — extend expiry before it lapses (Timer trigger calls this every ~47 h)
  3. delete()      — remove a subscription during cleanup
  4. list_active() — list all active subscriptions for this application

Authentication:
  Uses an app-only access token obtained via ``get_app_token()`` in graph_client.py
  (client credentials flow — no user interaction required).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from src.config import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Graph allows a maximum of 4320 minutes (3 days) for Mail.Read subscriptions.
# We request 2 days (2880 min) and renew every ~47 hours via the Timer trigger.
_DEFAULT_EXPIRY_MINUTES = 2880


class WebhookSubscriptionManager:
    """
    Manages Microsoft Graph API change-notification subscriptions.

    Usage::
        mgr = WebhookSubscriptionManager()

        # Register once (or re-register after expiry)
        sub_id = mgr.register(
            resource=f"users/{user_id}/mailFolders/Inbox/messages",
            change_types=["created"],
            notification_url="https://<func>.azurewebsites.net/api/graph_webhook",
        )

        # Renew from the Timer trigger every ~47 hours
        mgr.renew(sub_id)

        # Clean up when done
        mgr.delete(sub_id)
    """

    # ── Token ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_token() -> str:
        """Acquire an app-only (client credentials) access token."""
        from src.services.graph_client import get_app_token
        return get_app_token()

    # ── Register ──────────────────────────────────────────────────────────────

    def register(
        self,
        resource: str,
        change_types: List[str],
        notification_url: str,
        expiry_minutes: int = _DEFAULT_EXPIRY_MINUTES,
        client_state: Optional[str] = None,
    ) -> Optional[str]:
        """
        Create a new Graph API change-notification subscription.

        Args:
            resource:         Graph resource path.
                              e.g. "users/{user_id}/mailFolders/Inbox/messages"
            change_types:     List of change types, e.g. ["created"].
            notification_url: Public HTTPS endpoint to receive notifications
                              (must be reachable by Microsoft — use Function App URL).
            expiry_minutes:   Lifetime in minutes (max 4320 for Mail.Read).
            client_state:     Secret string echoed back in every notification for
                              validation. Defaults to first 16 chars of client secret.

        Returns:
            Subscription ID string on success, None on failure.
        """
        expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
        ).isoformat()

        state = client_state or (Config.AZURE_AD_CLIENT_SECRET or "")[:16]
        payload: Dict[str, Any] = {
            "changeType":         ",".join(change_types),
            "notificationUrl":    notification_url,
            "resource":           resource,
            "expirationDateTime": expiry,
            "clientState":        state,
        }

        try:
            token = self._get_token()
            resp = httpx.post(
                f"{GRAPH_BASE_URL}/subscriptions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
                content=json.dumps(payload),
                timeout=30,
            )
            resp.raise_for_status()
            data   = resp.json()
            sub_id = data.get("id")
            logger.info(
                "Subscription created: id=%s resource=%s expiry=%s",
                sub_id, resource, expiry,
            )
            return sub_id

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Failed to create subscription: HTTP %s — %s",
                exc.response.status_code,
                exc.response.text,
            )
            return None
        except Exception as exc:
            logger.error("Unexpected error creating subscription: %s", exc)
            return None

    # ── Renew ─────────────────────────────────────────────────────────────────

    def renew(
        self,
        subscription_id: str,
        expiry_minutes: int = _DEFAULT_EXPIRY_MINUTES,
    ) -> bool:
        """
        Extend an existing subscription's expiry by ``expiry_minutes`` from now.

        Returns:
            True on success, False on failure.
        """
        expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
        ).isoformat()

        try:
            token = self._get_token()
            resp = httpx.patch(
                f"{GRAPH_BASE_URL}/subscriptions/{subscription_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
                content=json.dumps({"expirationDateTime": expiry}),
                timeout=30,
            )
            resp.raise_for_status()
            logger.info(
                "Subscription renewed: id=%s new_expiry=%s",
                subscription_id, expiry,
            )
            return True

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Failed to renew subscription %s: HTTP %s — %s",
                subscription_id, exc.response.status_code, exc.response.text,
            )
            return False
        except Exception as exc:
            logger.error("Unexpected error renewing subscription %s: %s", subscription_id, exc)
            return False

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete(self, subscription_id: str) -> bool:
        """
        Delete a Graph API subscription.

        Returns:
            True on success or if already gone (404).
        """
        try:
            token = self._get_token()
            resp = httpx.delete(
                f"{GRAPH_BASE_URL}/subscriptions/{subscription_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code == 404:
                logger.info("Subscription %s already gone (404)", subscription_id)
                return True
            resp.raise_for_status()
            logger.info("Subscription deleted: id=%s", subscription_id)
            return True

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Failed to delete subscription %s: HTTP %s — %s",
                subscription_id, exc.response.status_code, exc.response.text,
            )
            return False
        except Exception as exc:
            logger.error("Unexpected error deleting subscription %s: %s", subscription_id, exc)
            return False

    # ── List ──────────────────────────────────────────────────────────────────

    def list_active(self) -> List[Dict[str, Any]]:
        """
        Return all Graph subscriptions currently registered for this application.

        Returns:
            List of subscription dicts from the Graph API response.
        """
        try:
            token = self._get_token()
            resp = httpx.get(
                f"{GRAPH_BASE_URL}/subscriptions",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            resp.raise_for_status()
            subscriptions = resp.json().get("value", [])
            logger.info("Listed %d active subscription(s)", len(subscriptions))
            return subscriptions

        except Exception as exc:
            logger.error("Failed to list subscriptions: %s", exc)
            return []

    # ── Convenience ───────────────────────────────────────────────────────────

    def register_inbox(
        self,
        user_id: Optional[str] = None,
        notification_url: Optional[str] = None,
        client_state: Optional[str] = None,
    ) -> Optional[str]:
        """
        Shortcut: register a subscription for the configured mailbox Inbox.

        Falls back to ``Config.GRAPH_MAILBOX_USER_ID`` and
        ``Config.GRAPH_WEBHOOK_NOTIFICATION_URL`` if parameters are not supplied.
        """
        uid  = user_id          or getattr(Config, "GRAPH_MAILBOX_USER_ID", None)
        url  = notification_url or getattr(Config, "GRAPH_WEBHOOK_NOTIFICATION_URL", None)

        if not uid:
            raise ValueError(
                "user_id not supplied and GRAPH_MAILBOX_USER_ID not configured"
            )
        if not url:
            raise ValueError(
                "notification_url not supplied and GRAPH_WEBHOOK_NOTIFICATION_URL not configured"
            )

        resource = f"users/{uid}/mailFolders/Inbox/messages"
        return self.register(
            resource=resource,
            change_types=["created"],
            notification_url=url,
            client_state=client_state,
        )
