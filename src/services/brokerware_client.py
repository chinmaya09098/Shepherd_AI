"""
Brokerware TMS API client.

Handles OAuth 2.0 Client Credentials token lifecycle and shipment creation.
Token is fetched on first use and refreshed automatically when it expires.

API Endpoints:
  Auth:            POST {base_url}/connect/token
  CreateShipment:  POST {base_url}/api/clientv1/CreateShipmentAPI
  CustomerContacts: GET {base_url}/api/clientv1/CustomerContactsSummary?pageSize=&page=
"""
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
# Token cache (module-level — lives for the duration of the process)
# ---------------------------------------------------------------------------
_access_token: Optional[str] = None
_token_expires_at: float = 0.0       # epoch seconds
_TOKEN_EXPIRY_BUFFER: int = 60       # refresh 60 s before actual expiry


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

def _fetch_token() -> str:
    """Obtain a new Bearer token using OAuth 2.0 Client Credentials flow."""
    global _access_token, _token_expires_at

    url = f"{Config.BROKERWARE_BASE_URL}/connect/token"
    logger.info("Fetching new Brokerware access token")

    response = requests.post(
        url,
        data={
            "grant_type":    "client_credentials",
            "client_id":     Config.BROKERWARE_CLIENT_ID,
            "client_secret": Config.BROKERWARE_CLIENT_SECRET,
        },
        timeout=15,
    )
    response.raise_for_status()

    data = response.json()
    _access_token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    _token_expires_at = time.time() + expires_in - _TOKEN_EXPIRY_BUFFER

    logger.info(f"Brokerware token acquired, expires in {expires_in}s")
    return _access_token


def _get_token() -> str:
    """Return a valid Bearer token, refreshing if expired."""
    if _access_token is None or time.time() >= _token_expires_at:
        return _fetch_token()
    return _access_token


# ---------------------------------------------------------------------------
# CreateShipment
# ---------------------------------------------------------------------------

def create_shipment(
    shipment: Shipment,
    customer_id: Optional[int] = None,
) -> CreateShipmentResult:
    """
    Submit a shipment to Brokerware TMS via the CreateShipmentAPI endpoint.

    Args:
        shipment:    Extracted Shipment model (from OpenAI agent).
        customer_id: Brokerware customerId. Falls back to
                     Config.BROKERWARE_DEFAULT_CUSTOMER_ID if not provided.

    Returns:
        CreateShipmentResult with success flag, shipment ID, and raw response.
    """
    resolved_customer_id = customer_id or Config.BROKERWARE_DEFAULT_CUSTOMER_ID

    payload = format_client_json(shipment, customer_id=resolved_customer_id)
    url = f"{Config.BROKERWARE_BASE_URL}/api/clientv1/CreateShipmentAPI"

    logger.info(
        f"Creating Brokerware shipment for customerId={resolved_customer_id} "
        f"pickup={payload.get('shipperZip')} → drop={payload.get('consigneeZip')}"
    )

    try:
        token = _get_token()
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

        logger.info(f"Brokerware shipment created successfully: id={shipment_id}")
        return CreateShipmentResult(
            success=True,
            shipment_id=str(shipment_id) if shipment_id else None,
            raw_response=data,
        )

    except requests.HTTPError as e:
        error_body = ""
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text if e.response else str(e)

        logger.error(f"Brokerware API HTTP error {e.response.status_code}: {error_body}")
        return CreateShipmentResult(
            success=False,
            error=f"HTTP {e.response.status_code}: {error_body}",
        )

    except Exception as e:
        logger.error(f"Brokerware API request failed: {e}")
        return CreateShipmentResult(success=False, error=str(e))


# ---------------------------------------------------------------------------
# CustomerContactsSummary
# ---------------------------------------------------------------------------

def get_customer_contacts(page_size: int = 10000) -> list:
    """
    Fetch all customer contacts from Brokerware, paginating through every page.

    Calls GET /api/clientv1/CustomerContactsSummary with pageSize + page params
    and walks all pages using the `totalPages` value in the response.

    Args:
        page_size: Records per page (Brokerware allows 1–10000; invalid values
                   default to 10000). Default 10000 to fetch everything at once.

    Returns:
        A list of contact dicts, each like:
            {"clientId": int, "customerId": int, "email": str}
        Returns an empty list on failure (errors are logged, never raised).
    """
    url = f"{Config.BROKERWARE_BASE_URL}/api/clientv1/CustomerContactsSummary"
    contacts: list = []
    page = 1
    total_pages = 1

    try:
        token = _get_token()
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
                "Brokerware contacts page %d/%d fetched (%d records so far)",
                page, total_pages, len(contacts),
            )
            page += 1

        logger.info("Fetched %d Brokerware customer contact(s) total", len(contacts))
        return contacts

    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.json()
        except Exception:
            body = e.response.text if e.response else str(e)
        logger.error("Brokerware CustomerContactsSummary HTTP error %s: %s",
                     getattr(e.response, "status_code", "?"), body)
        return contacts  # return whatever pages we managed to fetch

    except Exception as e:
        logger.error("Brokerware CustomerContactsSummary request failed: %s", e)
        return contacts


# ---------------------------------------------------------------------------
# Customer matching (sender email -> customerId)
# ---------------------------------------------------------------------------
# Brokerware's CustomerContactsSummary gives us email -> customerId directly, so
# matching is a straight lookup (exact email first, then email domain) — no
# vector search needed. Contacts are cached in-process and refreshed on a TTL.

_contacts_cache: Optional[list] = None
_contacts_cached_at: float = 0.0
_CONTACTS_TTL: int = 3600  # seconds — refresh the contact list hourly


def _get_contacts_cached() -> list:
    """Return the Brokerware contact list, refreshing from the API on TTL expiry.

    On a fetch failure the previous cache is kept (never wiped), so a transient
    API blip doesn't break matching.
    """
    global _contacts_cache, _contacts_cached_at
    if _contacts_cache is None or (time.time() - _contacts_cached_at) > _CONTACTS_TTL:
        fetched = get_customer_contacts()
        if fetched:  # only replace the cache when we actually got data
            _contacts_cache = fetched
            _contacts_cached_at = time.time()
    return _contacts_cache or []


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
    e = (email or "").strip().lower()
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


def match_customer(sender_email: str, receiver_email: str = "") -> dict:
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
    """
    contacts = _get_contacts_cached()
    if not contacts:
        logger.warning("No Brokerware contacts available — cannot match customer")
        return {"matches": [], "is_broker_match": False}

    for email in (sender_email, receiver_email):
        result = _match_one_email(contacts, email)
        if result:
            matches, confident = result
            logger.info(
                "Brokerware customer match for '%s' → customerId=%s (%s)",
                email, matches[0]["customerId"],
                "confident" if confident else "ambiguous",
            )
            return {"matches": matches, "is_broker_match": confident}

    logger.info("No Brokerware customer match for sender=%s / receiver=%s",
                sender_email, receiver_email)
    return {"matches": [], "is_broker_match": False}


def is_configured() -> bool:
    """Return True when Brokerware credentials are present."""
    return bool(Config.BROKERWARE_CLIENT_ID and Config.BROKERWARE_CLIENT_SECRET)
