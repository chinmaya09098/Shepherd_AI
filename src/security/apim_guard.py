"""
APIM (Azure API Management) subscription key guard.

Every request routed through APIM arrives with the header
``Ocp-Apim-Subscription-Key`` (or the header named in
``APIM_SUBSCRIPTION_KEY_HEADER``).  The Function App validates this key so
that direct calls to the Function App URL — bypassing the APIM/WAF layer —
are rejected with HTTP 401.

Architecture note
-----------------
Microsoft Graph sends webhook notifications DIRECTLY to the Function App URL
(not via APIM).  The ``graph_webhook`` handler therefore uses
``require_apim_key(req, bypass=True)`` to skip this check.  The endpoint is
still protected by clientState validation and (optionally) HMAC-SHA256.

Setup in APIM
-------------
1. API Management → APIs → Add your Function App as a backend.
2. Products → Create a Product (e.g. "shepherd-ai") and a Subscription.
3. Copy the Subscription primary key.
4. Store it in Key Vault as ``apim-subscription-key``
   (loaded automatically via src/secrets.py).
5. Add to Function App Configuration:
       APIM_SUBSCRIPTION_KEY=<same key value>
   OR just set KEY_VAULT_URL and let Key Vault handle it.

APIM inbound policy — forward the key to the backend:
------------------------------------------------------
<inbound>
    <base />
    <set-header name="Ocp-Apim-Subscription-Key" exists-action="override">
        <value>@(context.Subscription.PrimaryKey)</value>
    </set-header>
</inbound>
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

import azure.functions as func

logger = logging.getLogger("shepherd_ai.security.apim_guard")


def require_apim_key(
    req: func.HttpRequest,
    *,
    bypass: bool = False,
) -> Optional[func.HttpResponse]:
    """Validate the APIM subscription key on an HTTP trigger request.

    Args:
        req:    The incoming Azure Functions HttpRequest.
        bypass: Pass ``True`` to skip the check (e.g. Graph webhook POST which
                Microsoft sends directly, not through APIM).

    Returns:
        ``None``       — key is valid; caller should continue processing.
        ``HttpResponse(401)`` — key missing or invalid; caller must return it.
        ``HttpResponse(503)`` — APIM key not yet configured (warns and passes
                                through so the app works during first deploy).
    """
    if bypass:
        return None

    from src.config import Config

    expected_key = Config.APIM_SUBSCRIPTION_KEY
    header_name  = Config.APIM_SUBSCRIPTION_KEY_HEADER

    if not expected_key:
        # Key not configured yet.
        # APIM_STRICT_ENFORCEMENT=true → reject all requests (hard block).
        # APIM_STRICT_ENFORCEMENT=false (default) → warn and pass through so
        # the app stays functional during initial deployment before the vault
        # secret is populated.
        from src.config import Config
        if Config.APIM_STRICT_ENFORCEMENT:
            logger.error(
                "APIM_SUBSCRIPTION_KEY not configured — strict enforcement blocks all requests. "
                "Store the key in Key Vault as 'apim-subscription-key'."
            )
            _emit_auth_rejection(req, reason="apim_key_not_configured")
            return func.HttpResponse(
                "Service Unavailable — security misconfiguration",
                status_code=503,
                mimetype="text/plain",
            )
        logger.warning(
            "APIM_SUBSCRIPTION_KEY is not configured — all requests accepted. "
            "Store the key in Key Vault as 'apim-subscription-key' to enforce APIM routing."
        )
        return None

    received = req.headers.get(header_name, "").strip()

    if not received:
        logger.warning(
            "Request rejected (missing APIM key): method=%s url=%s",
            req.method, req.url,
        )
        _emit_auth_rejection(req, reason="missing_apim_key")
        return func.HttpResponse(
            "Unauthorized — subscription key required",
            status_code=401,
            mimetype="text/plain",
        )

    # Constant-time comparison prevents timing-based key enumeration.
    if not hmac.compare_digest(received.encode(), expected_key.encode()):
        logger.warning(
            "Request rejected (invalid APIM key): method=%s url=%s",
            req.method, req.url,
        )
        _emit_auth_rejection(req, reason="invalid_apim_key")
        return func.HttpResponse(
            "Unauthorized — invalid subscription key",
            status_code=401,
            mimetype="text/plain",
        )

    return None


def _emit_auth_rejection(req: func.HttpRequest, reason: str) -> None:
    """Fire-and-forget audit event; never raises."""
    try:
        from src.security.audit import emit
        emit(
            "apim_auth_rejection",
            {
                "reason":  reason,
                "method":  req.method,
                "url":     req.url,
                "client_ip": req.headers.get("X-Forwarded-For", "unknown"),
            },
        )
    except Exception:
        pass
