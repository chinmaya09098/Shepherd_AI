"""
Brokerware TMS API client.

Handles OAuth 2.0 token lifecycle and shipment creation / customer contact lookup.
Tokens are fetched on first use and refreshed automatically when expired.

Multi-tenant: tenant config is resolved from the receiving mailbox UPN via
BROKERWARE_MAILBOX_TENANT_MAP.  Per-tenant credentials are read from
BROKERWARE_<KEY>_* env vars.

API Endpoints:
  Auth (client creds):  POST {base_url}/connect/token  (grant_type=client_credentials)
  Auth (password):      POST {base_url}/connect/token  (grant_type=password)
  CreateShipment:       POST {base_url}/api/clientv1/CreateShipmentAPI
  List customers:       GET  {base_url}/api/client/{brokerClientId}/customer
  List contacts:        GET  {base_url}/api/client/{brokerClientId}/customer/{customerId}/contact
"""
import json as _json
import os
import time
import requests
from dataclasses import dataclass
from typing import Optional

from src.config import Config
from src.models.client_format import format_client_json
from src.models.shipment import Shipment
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------

@dataclass
class _BrokerwareTenant:
    key: str            # e.g. "shepherd", "shepherdwest", "default"
    base_url: str
    client_id: str
    client_secret: str
    broker_client_id: str = ""   # Brokerware company ID (e.g. "4097939")
    username: str = ""           # for password-grant contact lookup
    password: str = ""           # for password-grant contact lookup


def _extract_email_addresses(raw: str) -> list:
    """Extract bare email addresses from a raw To/From header value.

    Handles:
      - Plain address:            "shepherd@3plsystems0.onmicrosoft.com"
      - Display-name format:      "Shepherd <shepherd@3plsystems0.onmicrosoft.com>"
      - Semicolon-separated list: "A <a@x.com>; B <b@y.com>"
    """
    from email.utils import parseaddr as _parseaddr
    results = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        _, addr = _parseaddr(part)
        if addr:
            results.append(addr.strip().lower())
        elif part:
            results.append(part.lower())
    return results


def _load_tenant(mailbox_upn: str = "") -> _BrokerwareTenant:
    """Resolve the Brokerware tenant config for a given receiving mailbox UPN.

    Looks up *mailbox_upn* (case-insensitive) in BROKERWARE_MAILBOX_TENANT_MAP
    to obtain a tenant key, then reads BROKERWARE_<KEY>_* env vars for that
    tenant.  Falls back to the legacy BROKERWARE_* vars when no mapping is
    found.

    *mailbox_upn* may be a plain address, a "Name <addr>" string, or a
    semicolon-separated list of recipients — all forms are handled.
    """
    try:
        tenant_map = _json.loads(Config.BROKERWARE_MAILBOX_TENANT_MAP or "{}")
    except Exception:
        tenant_map = {}

    # Try each address extracted from the raw To header until we find a match.
    key = ""
    for addr in _extract_email_addresses(mailbox_upn):
        key = tenant_map.get(addr, "")
        if key:
            break

    if key:
        prefix = f"BROKERWARE_{key.upper()}_"
        base_url         = os.getenv(f"{prefix}BASE_URL")         or Config.BROKERWARE_BASE_URL
        client_id        = os.getenv(f"{prefix}CLIENT_ID")        or Config.BROKERWARE_CLIENT_ID or ""
        client_secret    = os.getenv(f"{prefix}CLIENT_SECRET")    or Config.BROKERWARE_CLIENT_SECRET or ""
        broker_client_id = os.getenv(f"{prefix}BROKER_CLIENT_ID") or ""
        username         = os.getenv(f"{prefix}USERNAME")         or ""
        password         = os.getenv(f"{prefix}PASSWORD")         or ""
        return _BrokerwareTenant(key=key, base_url=base_url,
                                 client_id=client_id, client_secret=client_secret,
                                 broker_client_id=broker_client_id,
                                 username=username, password=password)

    # Default / fallback tenant
    return _BrokerwareTenant(
        key="default",
        base_url=Config.BROKERWARE_BASE_URL,
        client_id=Config.BROKERWARE_CLIENT_ID or "",
        client_secret=Config.BROKERWARE_CLIENT_SECRET or "",
    )


# ---------------------------------------------------------------------------
# Per-tenant token cache (module-level — lives for the duration of the process)
# ---------------------------------------------------------------------------
# _tenant_tokens:          { tenant_key -> {"token": str, "expires_at": float} }
# _tenant_tokens_password: { tenant_key -> {"token": str, "expires_at": float} }
_tenant_tokens: dict = {}
_tenant_tokens_password: dict = {}
_TOKEN_EXPIRY_BUFFER: int = 60   # refresh 60 s before actual expiry


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CreateShipmentResult:
    success: bool
    shipment_id: Optional[str] = None   # ID returned by Brokerware on success
    raw_response: Optional[dict] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _fetch_token(tenant: _BrokerwareTenant) -> str:
    """Obtain a new Bearer token for *tenant* using OAuth 2.0 Client Credentials."""
    url = f"{tenant.base_url}/connect/token"
    logger.info("Fetching Brokerware access token for tenant=%s", tenant.key)

    response = requests.post(
        url,
        data={
            "grant_type":    "client_credentials",
            "client_id":     tenant.client_id,
            "client_secret": tenant.client_secret,
        },
        timeout=15,
    )
    response.raise_for_status()

    data = response.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    _tenant_tokens[tenant.key] = {
        "token":      token,
        "expires_at": time.time() + expires_in - _TOKEN_EXPIRY_BUFFER,
    }
    logger.info("Brokerware token for tenant=%s acquired, expires_in=%ss", tenant.key, expires_in)
    return token


def _get_token(tenant: _BrokerwareTenant) -> str:
    """Return a valid client-credentials Bearer token for *tenant*."""
    cached = _tenant_tokens.get(tenant.key)
    if not cached or time.time() >= cached["expires_at"]:
        return _fetch_token(tenant)
    return cached["token"]


def _get_token_password(tenant: _BrokerwareTenant) -> Optional[str]:
    """Return a password-grant Bearer token for *tenant* (used for contact lookup).

    Returns None when username/password are not configured.
    """
    if not tenant.username or not tenant.password:
        return None
    cached = _tenant_tokens_password.get(tenant.key)
    if cached and time.time() < cached["expires_at"]:
        return cached["token"]
    url = f"{tenant.base_url}/connect/token"
    logger.info("Fetching Brokerware password-grant token for tenant=%s", tenant.key)
    resp = requests.post(url, data={
        "grant_type":    "password",
        "client_id":     tenant.client_id,
        "client_secret": tenant.client_secret,
        "username":      tenant.username,
        "password":      tenant.password,
        "scope":         "openid profile",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    _tenant_tokens_password[tenant.key] = {
        "token":      token,
        "expires_at": time.time() + expires_in - _TOKEN_EXPIRY_BUFFER,
    }
    logger.info("Brokerware password token for tenant=%s acquired", tenant.key)
    return token


# ---------------------------------------------------------------------------
# CreateShipment
# ---------------------------------------------------------------------------

# HTTP status codes that indicate a transient Brokerware-side error worth retrying.
_RETRYABLE_HTTP_STATUS = {500, 502, 503, 504}
# Delay in seconds before each successive attempt (index 0 = first attempt, no delay).
_RETRY_BACKOFF = [0, 2, 5]


def create_shipment(
    shipment: Shipment,
    customer_id: Optional[int] = None,
    mailbox_upn: str = "",
) -> CreateShipmentResult:
    """
    Submit a shipment to Brokerware TMS via the CreateShipmentAPI endpoint.

    Retries automatically on transient HTTP 5xx errors and network timeouts
    (up to 3 attempts with 2 s / 5 s backoff).  HTTP 4xx errors (bad payload,
    auth) are returned immediately without retrying.

    Args:
        shipment:    Extracted Shipment model (from OpenAI agent).
        customer_id: Brokerware customerId. Submission is skipped if None.
        mailbox_upn: Receiving mailbox UPN used to select the correct tenant.

    Returns:
        CreateShipmentResult with success flag, shipment ID, and raw response.
    """
    tenant = _load_tenant(mailbox_upn)

    payload = format_client_json(shipment, customer_id=customer_id)
    url = f"{tenant.base_url}/api/clientv1/CreateShipmentAPI"
    max_attempts = len(_RETRY_BACKOFF)

    logger.info(
        "Creating Brokerware shipment tenant=%s customerId=%s "
        "pickup=%s → drop=%s",
        tenant.key, customer_id,
        payload.get("shipperZip"), payload.get("consigneeZip"),
    )

    for attempt, delay in enumerate(_RETRY_BACKOFF, start=1):
        if delay:
            time.sleep(delay)

        try:
            token = _get_token(tenant)
            response = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            # Brokerware returns {"loadId": <int>} on success
            shipment_id = (
                data.get("loadId")
                or data.get("shipmentId")
                or data.get("ShipmentId")
                or data.get("id")
                or (str(data) if isinstance(data, (int, str)) else None)
            )

            logger.info("Brokerware shipment created successfully: id=%s", shipment_id)
            return CreateShipmentResult(
                success=True,
                shipment_id=str(shipment_id) if shipment_id else None,
                raw_response=data,
            )

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            error_body = ""
            try:
                error_body = e.response.json()
            except Exception:
                error_body = e.response.text if e.response else str(e)

            if status not in _RETRYABLE_HTTP_STATUS or attempt == max_attempts:
                logger.error(
                    "Brokerware API HTTP error %s (attempt %d/%d): %s",
                    status, attempt, max_attempts, error_body,
                )
                return CreateShipmentResult(
                    success=False,
                    error=f"HTTP {status}: {error_body}",
                )
            logger.warning(
                "Brokerware API HTTP %s on attempt %d/%d — retrying in %ds",
                status, attempt, max_attempts, _RETRY_BACKOFF[attempt],
            )

        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == max_attempts:
                logger.error(
                    "Brokerware API request failed after %d attempt(s): %s",
                    max_attempts, e,
                )
                return CreateShipmentResult(success=False, error=str(e))
            logger.warning(
                "Brokerware API attempt %d/%d failed (%s) — retrying in %ds",
                attempt, max_attempts, type(e).__name__, _RETRY_BACKOFF[attempt],
            )

        except Exception as e:
            logger.error("Brokerware API request failed: %s", e)
            return CreateShipmentResult(success=False, error=str(e))

    # Should be unreachable, but keeps type-checker happy.
    return CreateShipmentResult(success=False, error="Max retries exceeded")


# ---------------------------------------------------------------------------
# Customer contact lookup (real API: /api/client/{brokerClientId}/customer/...)
# ---------------------------------------------------------------------------

def get_customer_contacts(page_size: int = 10000, mailbox_upn: str = "") -> list:
    """
    Fetch all customer contacts from Brokerware using the real API endpoints.

    Step 1: GET /api/client/{brokerClientId}/customer
            → list of customers: [{id, name, ...}, ...]
    Step 2: For each customer, GET
            /api/client/{brokerClientId}/customer/{customerId}/contact?search=Active
            → {"contacts": [{email, ...}, ...]}

    Requires password-grant credentials (BROKERWARE_{KEY}_USERNAME / _PASSWORD)
    and the company ID (BROKERWARE_{KEY}_BROKER_CLIENT_ID).

    Args:
        page_size:   Unused — kept for backward-compat signature.
        mailbox_upn: Receiving mailbox UPN used to select the correct tenant.

    Returns:
        A list of contact dicts, each like:
            {"customerId": int, "customerName": str, "email": str,
             "emailDomain": str, "clientId": None}
        Returns an empty list on failure (errors are logged, never raised).
    """
    tenant = _load_tenant(mailbox_upn)

    # Prefer a password-grant token when USERNAME/PASSWORD are configured, but
    # fall back to the client-credentials token — the /api/client/{id}/customer
    # and /customer/{id}/contact endpoints work with client-credentials, and we
    # do not have Brokerware user (password-grant) credentials for every tenant.
    token = None
    try:
        token = _get_token_password(tenant)
    except Exception as _tok_err:
        logger.warning(
            "get_customer_contacts: password-grant token failed for tenant=%s (%s) — "
            "falling back to client-credentials", tenant.key, _tok_err,
        )

    if not token:
        try:
            token = _get_token(tenant)
            logger.info("get_customer_contacts: using client-credentials token for tenant=%s",
                        tenant.key)
        except Exception as _cc_err:
            logger.error("get_customer_contacts: client-credentials token FAILED for tenant=%s: %s",
                         tenant.key, _cc_err, exc_info=True)
            return []

    if not token:
        logger.warning("get_customer_contacts: no usable token for tenant=%s", tenant.key)
        return []

    if not tenant.broker_client_id:
        logger.warning(
            "get_customer_contacts: BROKERWARE_%s_BROKER_CLIENT_ID not set",
            tenant.key.upper(),
        )
        return []

    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    contacts: list = []

    try:
        # Step 1 — list all customers for this broker client
        customers_url = f"{tenant.base_url}/api/client/{tenant.broker_client_id}/customer"
        cr = requests.get(customers_url, headers=hdrs, timeout=30)
        cr.raise_for_status()

        raw = cr.json()
        customers = raw if isinstance(raw, list) else raw.get("customers", raw.get("data", []))
        logger.info(
            "Brokerware: found %d customer(s) for tenant=%s broker_client_id=%s",
            len(customers), tenant.key, tenant.broker_client_id,
        )

        # Step 2 — get contacts for each customer
        for cust in customers:
            cust_id   = cust.get("id") or cust.get("customerId")
            cust_name = cust.get("name") or cust.get("customerName") or ""
            if cust_id is None:
                continue

            contacts_url = (
                f"{tenant.base_url}/api/client/{tenant.broker_client_id}"
                f"/customer/{cust_id}/contact"
            )
            try:
                resp = requests.get(
                    contacts_url, params={"search": "Active"},
                    headers=hdrs, timeout=30,
                )
                # Skip non-JSON responses (Angular SPA catch-all etc.)
                ct = resp.headers.get("Content-Type", "")
                if "json" not in ct:
                    logger.debug(
                        "Brokerware contacts for customerId=%s returned non-JSON (%s) — skipping",
                        cust_id, ct,
                    )
                    continue
                resp.raise_for_status()
                raw_contacts = resp.json()
                # Endpoint returns either a list directly or {"contacts": [...]}
                c_list = (
                    raw_contacts if isinstance(raw_contacts, list)
                    else raw_contacts.get("contacts", [])
                )
                for c in c_list:
                    email = (c.get("email") or "").strip()
                    contacts.append({
                        "customerId":   cust_id,
                        "customerName": cust_name,
                        "email":        email,
                        "emailDomain":  email.split("@")[-1].lower() if "@" in email else "",
                        "clientId":     None,
                    })
            except Exception as inner_e:
                logger.warning(
                    "Brokerware: failed to fetch contacts for customerId=%s tenant=%s: %s",
                    cust_id, tenant.key, inner_e,
                )

        logger.info(
            "Fetched %d Brokerware customer contact(s) total tenant=%s",
            len(contacts), tenant.key,
        )
        return contacts

    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.json()
        except Exception:
            body = e.response.text if e.response else str(e)
        logger.error(
            "Brokerware customer list HTTP error %s: %s (tenant=%s)",
            getattr(e.response, "status_code", "?"), body, tenant.key,
        )
        return contacts

    except Exception as e:
        logger.error(
            "Brokerware customer list request failed: %s (tenant=%s)", e, tenant.key,
        )
        return contacts


# ---------------------------------------------------------------------------
# Customer matching (sender email -> customerId)
# ---------------------------------------------------------------------------
# Brokerware's CustomerContactsSummary gives us email -> customerId directly, so
# matching is a straight lookup (exact email first, then email domain) — no
# vector search needed. Contacts are cached per-tenant and refreshed on a TTL.

# _tenant_contacts_cache: { tenant_key -> {"contacts": list, "cached_at": float} }
_tenant_contacts_cache: dict = {}
_CONTACTS_TTL: int = 3600  # seconds — refresh the contact list hourly


def _get_contacts_cached(mailbox_upn: str = "") -> list:
    """Return the Brokerware contact list for the tenant of *mailbox_upn*.

    Refreshes from the API on TTL expiry.  On a fetch failure the previous
    cache is kept (never wiped), so a transient API blip doesn't break matching.
    """
    tenant = _load_tenant(mailbox_upn)
    cached = _tenant_contacts_cache.get(tenant.key)
    if not cached or (time.time() - cached.get("cached_at", 0)) > _CONTACTS_TTL:
        try:
            fetched = get_customer_contacts(mailbox_upn=mailbox_upn)
        except Exception as _ce:
            logger.error("_get_contacts_cached: unexpected error for tenant=%s: %s", tenant.key, _ce, exc_info=True)
            fetched = []
        if fetched:
            _tenant_contacts_cache[tenant.key] = {
                "contacts":  fetched,
                "cached_at": time.time(),
            }
    entry = _tenant_contacts_cache.get(tenant.key)
    return entry["contacts"] if entry else []


def _to_match(contact: dict) -> dict:
    """Shape a Brokerware contact into the match dict the pipeline expects."""
    email = (contact.get("email") or "").strip()
    return {
        "customerId":   contact.get("customerId"),
        "customerName": contact.get("customerName") or None,
        "email":        email,
        "emailDomain":  contact.get("emailDomain") or (email.split("@")[-1].lower() if "@" in email else ""),
        "clientId":     contact.get("clientId"),
    }


def _match_one_email(contacts: list, email: str):
    """Try to match a single email address against the contacts.

    Returns (matches, is_confident) or None if no match:
      - exact email hit                → ([one match], True)   confident
      - single customer for the domain → ([one match], True)   confident
      - multiple customers for domain  → None (ambiguous — don't guess a wrong id)

    Note: Brokerware keys customers by individual email, and several emails can
    share one domain while mapping to different customerIds. So exact email is
    the reliable signal; an ambiguous domain resolves to no match (safer than a
    wrong customerId).
    """
    # Parse "Display Name <addr@domain.com>" format (RFC 2822 header value).
    from email.utils import parseaddr as _parseaddr
    _, parsed = _parseaddr(email or "")
    e = (parsed or email or "").strip().lower()
    if not e:
        return None

    # 1. Exact email match — most reliable.
    for c in contacts:
        if (c.get("email") or "").strip().lower() == e:
            return [_to_match(c)], True

    # 2. Domain match — only if the whole domain maps to ONE customer.
    domain = e.split("@")[-1] if "@" in e else ""
    if domain:
        domain_hits = [c for c in contacts
                       if (c.get("email") or "").strip().lower().endswith("@" + domain)]
        distinct_ids = {c.get("customerId") for c in domain_hits}
        if len(distinct_ids) == 1:
            return [_to_match(domain_hits[0])], True
        if len(distinct_ids) > 1:
            logger.info(
                "Domain '%s' maps to %d different customers — ambiguous, no match",
                domain, len(distinct_ids),
            )

    return None


def _match_from_static_map(sender_email: str, tenant_key: str) -> Optional[dict]:
    """
    Check BROKERWARE_CUSTOMER_EMAIL_MAP for a static email → customer_id mapping.

    Returns a match dict compatible with match_customer's return shape, or None.
    Map format (env var JSON):
        { "jack3pl@outlook.com": {"shepherd": 4098014, "shepherdwest": 5001} }
    """
    import json as _json
    try:
        raw = Config.BROKERWARE_CUSTOMER_EMAIL_MAP or "{}"
        email_map: dict = _json.loads(raw)
    except Exception:
        return None

    normalized = (sender_email or "").strip().lower()
    entry = email_map.get(normalized) or email_map.get(sender_email or "")
    if not entry:
        return None

    customer_id = entry.get(tenant_key)
    if customer_id is None:
        return None

    logger.info(
        "Static map match: email=%s tenant=%s → customerId=%s",
        normalized, tenant_key, customer_id,
    )
    return {
        "matches": [{
            "customerId":   int(customer_id),
            "customerName": "",
            "email":        normalized,
            "emailDomain":  normalized.split("@")[-1] if "@" in normalized else "",
            "clientId":     None,
        }],
        "is_broker_match": True,
    }


def match_customer(sender_email: str, receiver_email: str = "", mailbox_upn: str = "") -> dict:
    """
    Resolve an email to a Brokerware customer.

    Lookup order:
      1. BROKERWARE_CUSTOMER_EMAIL_MAP static config (always wins if set).
      2. Live CustomerContacts API (falls back gracefully when API returns nothing).

    Returns the shape the pipeline already consumes:
        {
          "matches": [ {customerId, customerName, email, emailDomain, clientId}, ... ],
          "is_broker_match": bool   # True = confident, use customerId directly
        }

    Args:
        mailbox_upn: Receiving mailbox UPN — selects the correct Brokerware tenant.
    """
    from urllib.parse import urlparse as _urlparse

    _tenant  = _load_tenant(mailbox_upn)
    _raw_url = _tenant.base_url or ""
    _parsed  = _urlparse(_raw_url if "://" in _raw_url else f"https://{_raw_url}")
    _domain  = _parsed.hostname or _raw_url

    def _seed_and_return(result: dict) -> dict:
        """Auto-seed the matched customer into customer_retry_config then return."""
        if result.get("is_broker_match"):
            try:
                from src.db.retry_config_repository import ensure_customer_exists
                for m in result["matches"]:
                    if m.get("customerId") is not None:
                        ensure_customer_exists(
                            customer_id=int(m["customerId"]),
                            client_id=int(m["clientId"]) if m.get("clientId") is not None else None,
                            domain=_domain,
                        )
            except Exception as _exc:
                logger.warning("match_customer: ensure_customer_exists failed: %s", _exc)
        return result

    # ── 1. Static map (highest priority) ─────────────────────────────────────
    static = _match_from_static_map(sender_email, _tenant.key)
    if static:
        return _seed_and_return(static)

    # ── 2. Live Brokerware CustomerContacts API ───────────────────────────────
    contacts = _get_contacts_cached(mailbox_upn)
    if not contacts:
        logger.warning("No Brokerware contacts available — cannot match customer")
        return {"matches": [], "is_broker_match": False}

    for email in (sender_email, receiver_email):
        result = _match_one_email(contacts, email)
        if result:
            matches, confident = result
            logger.info(
                "Brokerware API match for '%s' → customerId=%s (%s)",
                email, matches[0]["customerId"],
                "confident" if confident else "ambiguous",
            )
            return _seed_and_return({"matches": matches, "is_broker_match": confident})

    logger.info("No Brokerware customer match for sender=%s / receiver=%s",
                sender_email, receiver_email)
    return {"matches": [], "is_broker_match": False}


def is_configured(mailbox_upn: str = "") -> bool:
    """Return True when Brokerware credentials are present for the given mailbox tenant."""
    tenant = _load_tenant(mailbox_upn)
    return bool(tenant.client_id and tenant.client_secret)
