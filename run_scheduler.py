"""
Standalone scheduler entry point.

Starts the background scheduler and blocks until SIGINT / SIGTERM is received,
then shuts down gracefully.

Usage:
    python run_scheduler.py

Docker CMD example:
    CMD ["python", "run_scheduler.py"]

Environment variables (see src/config.py for full list):
    DATABASE_URL                     — PostgreSQL connection string
    BROKERWARE_MAILBOX_TENANT_MAP    — JSON map of mailbox UPN → tenant key
    BROKERWARE_<KEY>_*               — per-tenant Brokerware credentials
    AZURE_AD_*                       — Azure AD app credentials (Graph auth)
    GRAPH_WEBHOOK_NOTIFICATION_URL   — Function App webhook endpoint
    GRAPH_MAILBOX_USER_IDS           — comma-separated inbox UPNs
    ALERT_WEBHOOK_URL                — Teams / Slack webhook for health alerts
    ALERT_EMAIL_TO / ALERT_FROM_EMAIL — email alert recipient / sender
"""
import logging
import signal
import sys
import time

# Ensure the package root is on the path when running directly.
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from src.services.scheduler import start_scheduler, stop_scheduler
from src.utils.logger import get_logger

logger = get_logger("run_scheduler")

_running = True


def _handle_shutdown(signum, frame):
    global _running
    logger.info("Received signal %s — shutting down scheduler …", signum)
    _running = False


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    signal.signal(signal.SIGINT,  _handle_shutdown)
    # SIGTERM is not available on Windows — register only if present.
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_shutdown)

    logger.info("Starting Shepherd AI automation scheduler …")
    start_scheduler()
    logger.info("Scheduler running. Press Ctrl+C to stop.")

    # Block the main thread with a sleep loop (works on Windows and Linux).
    try:
        while _running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_scheduler()
        logger.info("Scheduler stopped.")
