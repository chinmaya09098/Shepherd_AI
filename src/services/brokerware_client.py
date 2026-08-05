"""
Brokerware TMS API client.

Handles OAuth 2.0 Client Credentials token lifecycle and shipment creation.
Tokens are fetched on first use and refreshed automatically when expired.

Multi-tenant: tenant config is resolved from the receiving mailbox UPN via
BROKERWARE_MAILBOX_TENANT_MAP.  Per-tenant credentials are read from
BROKERWARE_<KEY>_BASE_URL / _CLIENT_ID / _CLIENT_SECRET env vars.

API Endpoints:
  Auth:            POST {base_url}/connect/token
  CreateShipment:  POST {base_url}/api/clientv1/CreateShipmentAPI
  CustomerContacts: GET {base_url}/api/clientv1/CustomerContactsSummary?pageSize=&page=
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
    key: str          # e.g. "shepherd", "shepherdwest", "default"
    base_url: str
    client_id: str
    client_secret: str


def _load_tenant(mailbox_upn: str = "") -> _BrokerwareTenant:
    """Resolve the Brokerware tenant config for a given receiving mailbox UPN.

    Looks up *mailbox_upn* (case-insensitive) in BROKERWARE_MAILBOX_TENANT_MAP
    to obtain a tenant key, then reads BROKERWARE_<KEY>_* env vars for that
    tenant.  Falls back to the legacy BROKERWARE_* vars when no mapping is
    found.
    """
    try:
        tenant_map = _json.loads(Config.BROKERWARE_MAILBOX_TENANT_MAP or "{}")
    except Exception:
        tenant_map = {}

    key = tenant_map.get((mailbox_upn or "").strip().lower(), "")

    if key:
        prefix = f"BROKERWARE_{key.upper()}_"
        base_url      = os.getenv(f"{prefix}BASE_URL")      or Config.BROKERWARE_BASE_URL
        client_id     = os.getenv(f"{prefix}CLIENT_ID")     or Config.BROKERWARE_CLIENT_ID or ""
        client_secret = os.getenv(f"{prefix}CLIENT_SECRET") or Config.BROKERWARE_CLIENT_SECRET or ""
        return _BrokerwareTenant(key=key, base_url=base_url,
                                 client_id=client_id, client_secret=client_secret)

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
# _tenant_tokens: { tenant_key -> {"token": str, "expires_at": float} }
_tenant_tokens: dict = {}
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
    """Return a valid Bearer token for *tenant*, refreshing if expired."""
    cached = _tenant_tokens.get(tenant.key)
    if not cached or time.time() >= cached["expires_at"]:
        return _fetch_token(tenant)
    return cached["token"]


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
# CustomerContactsSummary
# ---------------------------------------------------------------------------

def get_customer_contacts(page_size: int = 10000, mailbox_upn: str = "") -> list:
    """
    Fetch all customer contacts from Brokerware, paginating through every page.

    Calls GET /api/clientv1/CustomerContactsSummary with pageSize + page params
    and walks all pages using the `totalPages` value in the response.

    Args:
        page_size:   Records per page (1–10000; invalid values default to 10000).
        mailbox_upn: Receiving mailbox UPN used to select the correct tenant.

    Returns:
        A list of contact dicts, each like:
            {"clientId": int, "customerId": int, "email": str}
        Returns an empty list on failure (errors are logged, never raised).
    """
    tenant = _load_tenant(mailbox_upn)
    url = f"{tenant.base_url}/api/clientv1/CustomerContactsSummary"
    contacts: list = []
    page = 1
    total_pages = 1

    try:
        token = _get_token(tenant)
        while page <= total_pages:
            response = requests.get(
                url,
                params={"pageSize": page_size, "page": page},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            contacts.extend(data.get("contacts", []))
            total_pages = data.get("totalPages", 1) or 1
            logger.info(
                "Brokerware contacts page %d/%d fetched (%d records so far) tenant=%s",
                page, total_pages, len(contacts), tenant.key,
            )
            page += 1

        logger.info("Fetched %d Brokerware customer contact(s) total tenant=%s",
                    len(contacts), tenant.key)
        return contacts

    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.json()
        except Exception:
            body = e.response.text if e.response else str(e)
        logger.error("Brokerware CustomerContactsSummary HTTP error %s: %s (tenant=%s)",
                     getattr(e.response, "status_code", "?"), body, tenant.key)
        return contacts  # return whatever pages we managed to fetch

    except Exception as e:
        logger.error("Brokerware CustomerContactsSummary request failed: %s (tenant=%s)",
                     e, tenant.key)
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
        fetched = get_customer_contacts(mailbox_upn=mailbox_upn)
        if fetched:
            _tenant_contacts_cache[tenant.key] = {
                "contacts":  fetched,
                "cached_at": time.time(),
            }
    entry = _tenant_contacts_cache.get(tenant.key)
    return entry["contacts"] if entry else []


def _to_match(contact: dict) -> dict:
    """Shape a Brokerware contact into the match dict the pipeline expects.

    (customerName is None — the Brokerware summary endpoint does not return it.)
    """
    email = (contact.get("email") or "").strip()
    return {
        "customerId":   contact.get("customerId"),
        "customerName": None,
        "email":        email,
        "emailDomain":  email.split("@")[-1].lower() if "@" in email else "",
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


def match_customer(sender_email: str, receiver_email: str = "", mailbox_upn: str = "") -> dict:
    """
    Resolve an email to a Brokerware customer by direct lookup against the
    CustomerContactsSummary list (replaces the Hyperion + vector-search path).

    Tries the sender first, then the receiver. Returns the same shape the
    pipeline already consumes:
        {
          "matches": [ {customerId, customerName, email, emailDomain, clientId}, ... ],
          "is_broker_match": bool   # True = confident, use customerId directly
        }
    An empty matches list means no confident customer was found (caller then
    treats customerId as None — a wrong id is worse than none).

    On a successful match, ensure_customer_exists() is called so the customer
    is automatically seeded into customer_retry_config even if sync_from_brokerware()
    has not been run yet (zero-config hot-path seeding).

    Args:
        mailbox_upn: Receiving mailbox UPN — selects the correct Brokerware tenant.
    """
    from urllib.parse import urlparse as _urlparse
    contacts = _get_contacts_cached(mailbox_upn)
    if not contacts:
        logger.warning("No Brokerware contacts available — cannot match customer")
        return {"matches": [], "is_broker_match": False}

    # Derive tenant domain once — used for auto-seeding the retry config.
    _tenant = _load_tenant(mailbox_upn)
    _raw_url = _tenant.base_url or ""
    _parsed  = _urlparse(_raw_url if "://" in _raw_url else f"https://{_raw_url}")
    _domain  = _parsed.hostname or _raw_url

    for email in (sender_email, receiver_email):
        result = _match_one_email(contacts, email)
        if result:
            matches, confident = result
            logger.info(
                "Brokerware customer match for '%s' -> customerId=%s (%s)",
                email, matches[0]["customerId"],
                "confident" if confident else "ambiguous",
            )
            if confident:
                # Auto-seed the customer into customer_retry_config (no-op if already present).
                try:
                    from src.db.retry_config_repository import ensure_customer_exists
                    for m in matches:
                        if m.get("customerId") is not None:
                            ensure_customer_exists(
                                customer_id=int(m["customerId"]),
                                client_id=int(m["clientId"]) if m.get("clientId") is not None else None,
                                domain=_domain,
                            )
                except Exception as _exc:
                    logger.warning("match_customer: ensure_customer_exists failed: %s", _exc)
            return {"matches": matches, "is_broker_match": confident}

    logger.info("No Brokerware customer match for sender=%s / receiver=%s",
                sender_email, receiver_email)
    return {"matches": [], "is_broker_match": False}


def is_configured(mailbox_upn: str = "") -> bool:
    """Return True when Brokerware credentials are present for the given mailbox tenant."""
    tenant = _load_tenant(mailbox_upn)
    return bool(tenant.client_id and tenant.client_secret)
