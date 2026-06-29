"""
subscription_renewal — Azure Function (Timer Trigger)

Renews expiring Microsoft Graph webhook subscriptions so the system continues
to receive real-time email notifications without interruption.

Graph subscriptions for mail resources expire after at most 4230 minutes
(~70.5 hours). This timer runs every 12 hours and renews any subscription
that will expire within the next 24 hours.

Schedule (cron): every 12 hours → 0 0 */12 * * *
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import azure.functions as func

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

logger = logging.getLogger("shepherd.subscription_renewal")

_RENEW_THRESHOLD_HOURS = 24     # Renew if expiring within this many hours


def main(mytimer: func.TimerRequest) -> None:
    """Timer trigger entry point — runs every 12 hours."""
    now = datetime.now(timezone.utc)
    if mytimer.past_due:
        logger.warning("subscription_renewal timer is past due")

    logger.info("subscription_renewal START %s", now.isoformat())

    # ── Load token ─────────────────────────────────────────────────────────────
    from function_app.shared.pipeline import (
        get_connection_string,
        get_graph_client,
        validate_and_get_token,
    )
    from src.services.webhook_service import WebhookService

    token, access_token = validate_and_get_token()
    if not access_token:
        logger.error("No Graph token — cannot renew subscriptions")
        return

    graph_client = get_graph_client()
    conn_str = get_connection_string()

    if not conn_str:
        logger.error("AZURE_STORAGE_CONNECTION_STRING not set")
        return

    svc = WebhookService(connection_string=conn_str)
    subscriptions = svc.get_all_active()

    if not subscriptions:
        logger.info("No active subscriptions found")
        return

    threshold = now + timedelta(hours=_RENEW_THRESHOLD_HOURS)
    renewed = 0
    failed = 0

    for sub in subscriptions:
        if sub.expiration_date_time and sub.expiration_date_time > threshold:
            logger.debug(
                "Subscription %s not due for renewal (expires %s)",
                sub.subscription_id,
                sub.expiration_date_time.isoformat(),
            )
            continue

        logger.info(
            "Renewing subscription %s (expires %s)",
            sub.subscription_id,
            sub.expiration_date_time.isoformat() if sub.expiration_date_time else "unknown",
        )

        try:
            success = asyncio.run(
                svc.renew_subscription(sub.subscription_id, access_token)
            )
            if success:
                renewed += 1
            else:
                failed += 1
                # Mark as inactive if renewal fails repeatedly
                svc.deactivate(sub.subscription_id, sub.tenant_id)
        except Exception as exc:
            logger.error(
                "Failed to renew subscription %s: %s",
                sub.subscription_id,
                exc,
            )
            failed += 1

    logger.info(
        "subscription_renewal DONE: renewed=%d failed=%d total=%d",
        renewed,
        failed,
        len(subscriptions),
    )
