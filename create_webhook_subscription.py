"""
One-time script to create a Graph webhook subscription.

Run this AFTER:
  1. The webhook handler is running (uvicorn src.api.webhook_handler:app --port 8502)
  2. ngrok is exposing it publicly (ngrok http 8502)
  3. GRAPH_NOTIFICATION_URL is set in .env  (e.g. https://abc123.ngrok.io/webhook)
  4. GRAPH_ACCESS_TOKEN is set in .env (a valid delegated token with Subscriptions.ReadWrite.All)

Usage:
  python create_webhook_subscription.py
"""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from src.config import Config
from src.services.webhook_service import WebhookService


async def main():
    access_token = Config.GRAPH_ACCESS_TOKEN
    notification_url = Config.GRAPH_NOTIFICATION_URL

    if not access_token:
        print("ERROR: GRAPH_ACCESS_TOKEN not set in .env")
        return

    if not notification_url:
        print("ERROR: GRAPH_NOTIFICATION_URL not set in .env")
        print("Set it to your ngrok URL, e.g.: https://abc123.ngrok.io/webhook")
        return

    print(f"Creating subscription...")
    print(f"  Notification URL : {notification_url}")
    print(f"  Resource         : me/mailFolders/inbox/messages")

    svc = WebhookService(connection_string=Config.AZURE_STORAGE_CONNECTION_STRING)

    sub = await svc.create_subscription(
        access_token=access_token,
        notification_url=notification_url,
        resource="me/mailFolders/inbox/messages",
    )

    if sub:
        print(f"\nSubscription created successfully!")
        print(f"  Subscription ID : {sub.subscription_id}")
        print(f"  Expires         : {sub.expiration_date_time}")
        print(f"\nShepherd AI will now receive notifications when new emails arrive.")
    else:
        print("\nFailed to create subscription. Check logs for details.")
        print("Common reasons:")
        print("  - GRAPH_ACCESS_TOKEN expired or missing Subscriptions.ReadWrite.All")
        print("  - GRAPH_NOTIFICATION_URL is not publicly accessible")
        print("  - Admin consent not granted for Subscriptions.ReadWrite.All")


if __name__ == "__main__":
    asyncio.run(main())
