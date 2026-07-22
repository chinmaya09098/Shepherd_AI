"""
Role-Based Access Control (RBAC) enforcement for Shepherd AI.

SOW requirement: "Role-Based Access Control (RBAC) enforcement across
application services, APIs, and tenant-scoped workflows."

How it works
------------
Azure AD app roles are defined in the Function App's App Registration
(Manifest → appRoles).  When a caller authenticates via OAuth 2.0 and
requests a token with the app's audience, Azure AD embeds their assigned
roles in the ``roles`` claim of the JWT.

For Azure Function HTTP triggers the caller passes a Bearer token in the
``Authorization`` header.  This module:
1. Extracts and decodes the JWT (signature NOT verified here — Azure AD
   App Service authentication / EasyAuth should verify the signature at
   the platform level, or use the ``validate_jwt`` APIM policy).
2. Reads the ``roles`` claim.
3. Compares against the required roles for the endpoint.

App roles to define in Azure Portal (App Registration → App roles)
-------------------------------------------------------------------
    ShipmentProcessor  — process emails and create shipments (service accounts)
    WebhookAdmin       — register / manage Graph webhook subscriptions
    ReadOnly           — view conversation state (future UI)

Usage in function_app.py
------------------------
    from src.security.rbac import require_role, RbacError

    @app.route(route="register_webhooks", auth_level=func.AuthLevel.FUNCTION)
    def register_webhooks(req):
        err = require_role(req, "WebhookAdmin")
        if err:
            return err
        ...
"""
from __future__ import annotations

import base64
import json
import logging
from typing import List, Optional

import azure.functions as func

logger = logging.getLogger("shepherd_ai.security.rbac")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def require_role(
    req: func.HttpRequest,
    *required_roles: str,
    skip_if_no_token: bool = True,
) -> Optional[func.HttpResponse]:
    """Verify the caller holds at least one of *required_roles*.

    Args:
        req:               The incoming HTTP request.
        *required_roles:   One or more role names (OR logic — any one is enough).
        skip_if_no_token:  If True and no Authorization header is present,
                           return None (allow through).  Set False to enforce
                           token presence strictly.

    Returns:
        None          — caller has the required role; proceed.
        HttpResponse  — 401 (no/invalid token) or 403 (wrong roles); return it.
    """
    auth_header = req.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        if skip_if_no_token:
            # No token provided — pass through (e.g. function-key protected
            # endpoints where the host key replaces a JWT).
            logger.debug("No Bearer token — RBAC check skipped (skip_if_no_token=True)")
            return None
        logger.warning("RBAC: missing Authorization header for %s %s", req.method, req.url)
        return func.HttpResponse(
            "Unauthorized — Bearer token required",
            status_code=401,
            mimetype="text/plain",
        )

    token = auth_header[len("Bearer "):]
    claims = _decode_jwt_claims(token)

    if claims is None:
        logger.warning("RBAC: could not decode JWT for %s %s", req.method, req.url)
        return func.HttpResponse(
            "Unauthorized — invalid token",
            status_code=401,
            mimetype="text/plain",
        )

    caller_roles: List[str] = claims.get("roles", [])
    if isinstance(caller_roles, str):
        caller_roles = [caller_roles]

    if required_roles and not any(r in caller_roles for r in required_roles):
        logger.warning(
            "RBAC: access denied for upn=%s roles=%s required=%s url=%s",
            claims.get("upn") or claims.get("preferred_username", "unknown"),
            caller_roles,
            list(required_roles),
            req.url,
        )
        _emit_access_denied(req, claims, list(required_roles))
        return func.HttpResponse(
            f"Forbidden — required role(s): {', '.join(required_roles)}",
            status_code=403,
            mimetype="text/plain",
        )

    logger.debug(
        "RBAC: access granted upn=%s roles=%s",
        claims.get("upn") or claims.get("preferred_username", "unknown"),
        caller_roles,
    )
    return None


def extract_tenant_id(req: func.HttpRequest) -> Optional[str]:
    """Extract the Azure AD tenant ID from a Bearer JWT (``tid`` claim).

    Returns None if no token or claim is present.
    """
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    claims = _decode_jwt_claims(auth_header[len("Bearer "):])
    return claims.get("tid") if claims else None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode_jwt_claims(token: str) -> Optional[dict]:
    """Decode the payload of a JWT WITHOUT verifying the signature.

    Signature verification must be performed by EasyAuth or the APIM
    ``validate-jwt`` policy before the request reaches this code.
    This function is only used to READ claims, not to authenticate the token.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        # Add padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception as exc:
        logger.debug("JWT decode failed: %s", exc)
        return None


def _emit_access_denied(req: func.HttpRequest, claims: dict, required: List[str]) -> None:
    try:
        from src.security.audit import emit
        emit(
            "rbac_access_denied",
            {
                "upn":           claims.get("upn") or claims.get("preferred_username", ""),
                "oid":           claims.get("oid", ""),
                "tid":           claims.get("tid", ""),
                "caller_roles":  ", ".join(claims.get("roles", [])),
                "required_roles": ", ".join(required),
                "url":           req.url,
                "method":        req.method,
            },
        )
    except Exception:
        pass
