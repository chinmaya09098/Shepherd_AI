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
import time as _time
from typing import List, Optional

import azure.functions as func

logger = logging.getLogger("shepherd_ai.security.rbac")

# ---------------------------------------------------------------------------
# JWKS in-process cache (refreshed every AZURE_AD_JWKS_CACHE_TTL seconds)
# ---------------------------------------------------------------------------
_jwks_cache: dict = {}
_jwks_cached_at: float = 0.0


# ---------------------------------------------------------------------------
# JWKS fetch + JWT signature verification
# ---------------------------------------------------------------------------

def _fetch_jwks(tenant_id: str) -> dict:
    """Fetch and in-process-cache Azure AD JWKS keys.

    Keys are refreshed once per AZURE_AD_JWKS_CACHE_TTL seconds (default 1 h).
    On network failure the last cached value is returned; on first-call failure
    an empty dict is returned and signature verification is skipped.
    """
    global _jwks_cache, _jwks_cached_at
    from src.config import Config
    ttl = Config.AZURE_AD_JWKS_CACHE_TTL
    now = _time.time()
    if _jwks_cache and (now - _jwks_cached_at) < ttl:
        return _jwks_cache
    url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    try:
        import httpx
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_cached_at = now
        return _jwks_cache
    except Exception as exc:
        logger.warning(
            "JWKS fetch failed (%s) — JWT signature verification will be skipped "
            "this cycle; cached keys (if any) still used.", exc
        )
        return _jwks_cache  # return stale cache if available, else {}


def _verify_jwt_signature(token: str) -> Optional[dict]:
    """Verify an RS256 JWT against Azure AD JWKS and return decoded claims.

    Behaviour matrix:
    - PyJWT installed + JWKS available  → full RS256 verification (issuer, audience, expiry)
    - PyJWT installed + JWKS unavailable → degraded: unverified decode (logged as warning)
    - PyJWT not installed               → degraded: unverified decode (logged as warning)
    - Any verification error            → returns None (caller returns 401/403)

    Signature verification MUST be the last line of defence only when APIM's
    ``validate-jwt`` inbound policy is also active.  This function adds a
    defence-in-depth layer so the Function App itself is not relying solely
    on upstream infrastructure.
    """
    try:
        import jwt as pyjwt
        from jwt.algorithms import RSAAlgorithm
    except ImportError:
        logger.warning(
            "PyJWT not installed — JWT signature not verified. "
            "Run: pip install PyJWT>=2.8.0"
        )
        return _decode_jwt_claims(token)  # fall back to unverified decode

    # Decode header to get key ID (kid) without verifying the signature yet
    try:
        header = pyjwt.get_unverified_header(token)
    except Exception:
        logger.warning("JWT header decode failed — rejecting token")
        return None

    kid = header.get("kid")
    from src.config import Config
    tenant_id = Config.AZURE_AD_TENANT_ID or "common"
    audience  = Config.AZURE_AD_AUDIENCE  or Config.AZURE_AD_CLIENT_ID

    jwks_data = _fetch_jwks(tenant_id)
    if not jwks_data:
        logger.warning(
            "JWKS unavailable and no cache — falling back to unverified JWT decode (degraded)"
        )
        return _decode_jwt_claims(token)

    # Find the matching public key by kid
    public_key = None
    for key_data in jwks_data.get("keys", []):
        if key_data.get("kid") == kid:
            try:
                public_key = RSAAlgorithm.from_jwk(key_data)
            except Exception as exc:
                logger.warning("JWK parse error for kid=%s: %s", kid, exc)
            break

    if public_key is None:
        logger.warning(
            "No matching JWK found for kid=%s — rejecting token "
            "(key may have rotated; JWKS cache will refresh next cycle)", kid
        )
        return None

    # Full verification: signature, expiry, audience, issuer
    issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    try:
        claims = pyjwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"verify_exp": True},
        )
        return claims
    except pyjwt.ExpiredSignatureError:
        logger.warning("JWT expired — rejecting token")
        return None
    except pyjwt.InvalidAudienceError:
        logger.warning("JWT audience mismatch — rejecting token (expected audience=%s)", audience)
        return None
    except pyjwt.InvalidIssuerError:
        logger.warning("JWT issuer mismatch — rejecting token (expected issuer=%s)", issuer)
        return None
    except Exception as exc:
        logger.warning("JWT verification failed: %s — rejecting token", exc)
        return None


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
    # _verify_jwt_signature performs full RS256 verification (signature, expiry,
    # audience, issuer) when PyJWT is installed and Azure AD JWKS is reachable.
    # Degrades gracefully to unverified decode when neither is available.
    claims = _verify_jwt_signature(token)

    if claims is None:
        logger.warning("RBAC: JWT verification failed for %s %s", req.method, req.url)
        return func.HttpResponse(
            "Unauthorized — invalid or expired token",
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
