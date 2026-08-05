"""
Health monitor — periodic system checks with automatic alerting.

Checks:
  - check_brokerware_auth : Can we obtain tokens for every configured tenant?
  - check_db_connection   : Is the PostgreSQL DB reachable?
  - check_graph_auth      : Can we obtain an app-only Graph token?

Alert channels (both optional; configured via env vars):
  - HTTP webhook  POST to ALERT_WEBHOOK_URL (Teams / Slack / generic).
  - Email         Graph sendMail from ALERT_FROM_EMAIL to ALERT_EMAIL_TO.

Usage::
    from src.services.health_monitor import run_checks

    # Called by the scheduler every 15 minutes
    run_checks()
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import requests

from src.config import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class HealthCheckResult:
    name:   str
    ok:     bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_brokerware_auth() -> HealthCheckResult:
    """Try to fetch a Brokerware token for every configured tenant."""
    import json as _json
    from src.services.brokerware_client import _load_tenant, _fetch_token

    try:
        tenant_map: dict = _json.loads(Config.BROKERWARE_MAILBOX_TENANT_MAP or "{}")
    except Exception:
        tenant_map = {}

    # Collect unique tenant keys; always include the default tenant.
    tenant_keys = list({v for v in tenant_map.values()} | {"default"})
    failures: List[str] = []

    for key in tenant_keys:
        # Build a representative UPN for _load_tenant: use the first mapped UPN
        # for named keys, empty string for the default fallback.
        upn = next((u for u, k in tenant_map.items() if k == key), "")
        tenant = _load_tenant(upn)
        if not tenant.client_id or not tenant.client_secret:
            continue  # unconfigured tenant — skip gracefully
        try:
            _fetch_token(tenant)
        except Exception as exc:
            failures.append(f"{tenant.key}: {exc}")

    if failures:
        return HealthCheckResult(
            name="brokerware_auth",
            ok=False,
            detail="; ".join(failures),
        )
    return HealthCheckResult(name="brokerware_auth", ok=True)


def check_db_connection() -> HealthCheckResult:
    """Verify the PostgreSQL database is reachable."""
    from src.db.database import init_db, get_session
    from sqlalchemy import text

    if not init_db():
        return HealthCheckResult(
            name="db_connection",
            ok=False,
            detail="init_db() returned False — DB may be unconfigured",
        )
    try:
        with get_session() as session:
            session.execute(text("SELECT 1"))
        return HealthCheckResult(name="db_connection", ok=True)
    except Exception as exc:
        return HealthCheckResult(name="db_connection", ok=False, detail=str(exc))


def check_graph_auth() -> HealthCheckResult:
    """Verify we can obtain an app-only Graph token."""
    try:
        from src.services.graph_client import get_app_token
        token = get_app_token()
        if not token:
            return HealthCheckResult(
                name="graph_auth",
                ok=False,
                detail="get_app_token() returned empty token",
            )
        return HealthCheckResult(name="graph_auth", ok=True)
    except Exception as exc:
        return HealthCheckResult(name="graph_auth", ok=False, detail=str(exc))


# ---------------------------------------------------------------------------
# Alert delivery
# ---------------------------------------------------------------------------

def _build_alert_body(failures: List[HealthCheckResult]) -> str:
    """Human-readable alert message."""
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"[Shepherd AI] Health alert — {ts}", ""]
    for r in failures:
        lines.append(f"  FAIL  {r.name}: {r.detail}")
    return "\n".join(lines)


def send_alert(failures: List[HealthCheckResult]) -> None:
    """Dispatch alert via all configured channels.

    Both channels are optional and independent — a failure in one does not
    suppress the other.
    """
    if not failures:
        return

    body = _build_alert_body(failures)
    logger.error("HEALTH ALERT:\n%s", body)

    _send_webhook_alert(body, failures)
    _send_email_alert(body)


def _send_webhook_alert(body: str, failures: List[HealthCheckResult]) -> None:
    """POST a JSON payload to ALERT_WEBHOOK_URL (Teams / Slack compatible)."""
    url = Config.ALERT_WEBHOOK_URL
    if not url:
        return

    # Teams adaptive-card / Slack-compatible simple format
    payload = {
        "text":    body,
        "summary": f"Shepherd AI health alert: {', '.join(r.name for r in failures)}",
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Health alert webhook sent (%s)", resp.status_code)
    except Exception as exc:
        logger.error("Failed to send health alert webhook: %s", exc)


def _send_email_alert(body: str) -> None:
    """Send an alert email via Microsoft Graph sendMail API."""
    sender = Config.ALERT_FROM_EMAIL
    to     = Config.ALERT_EMAIL_TO
    if not sender or not to:
        return

    try:
        from src.services.graph_client import get_app_token
        import httpx

        token   = get_app_token()
        subject = "Shepherd AI — Health Check Alert"
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": False,
        }
        resp = httpx.post(
            f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            content=json.dumps(payload),
            timeout=30,
        )
        resp.raise_for_status()
        logger.info("Health alert email sent to %s", to)
    except Exception as exc:
        logger.error("Failed to send health alert email: %s", exc)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_checks() -> List[HealthCheckResult]:
    """Run all health checks and alert on any failure.

    Returns the full list of results (both passing and failing).
    Designed to be called from the scheduler every 15 minutes.
    """
    checks = [
        check_db_connection,
        check_brokerware_auth,
        check_graph_auth,
    ]

    results: List[HealthCheckResult] = []
    for check_fn in checks:
        try:
            result = check_fn()
        except Exception as exc:
            result = HealthCheckResult(
                name=getattr(check_fn, "__name__", str(check_fn)),
                ok=False,
                detail=f"Unexpected error: {exc}",
            )
        results.append(result)
        status = "OK" if result.ok else "FAIL"
        logger.info("health_check %-20s %s  %s", result.name, status, result.detail or "")

    failures = [r for r in results if not r.ok]
    if failures:
        send_alert(failures)

    return results
