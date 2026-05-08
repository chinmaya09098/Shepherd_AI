"""
Hyperion TMS API client.

Handles OAuth 2.0 Client Credentials token lifecycle and customer contact
lookups. The token is fetched on first use and refreshed automatically when
it expires.
"""
import time
import requests
from typing import Optional
from src.config import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Token cache (module-level — lives for the duration of the process)
# ---------------------------------------------------------------------------
_access_token: Optional[str] = None
_token_expires_at: float = 0.0          # epoch seconds
_TOKEN_EXPIRY_BUFFER: int = 60          # refresh 60 s before actual expiry

# Contacts cache — fetched once per token lifecycle
_contacts_cache: Optional[list] = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _fetch_token() -> str:
    """
    Obtain a new Bearer token using the OAuth 2.0 Client Credentials flow.
    Credentials are sent as a Basic Auth header as required by the API.
    """
    global _access_token, _token_expires_at, _contacts_cache

    url = "https://3pl.hyperiontms.com/connect/token"
    logger.info("Fetching new Hyperion TMS access token")

    response = requests.post(
        url,
        data={"grant_type": "client_credentials"},
        auth=(Config.HYPERION_CLIENT_ID, Config.HYPERION_CLIENT_SECRET),
        timeout=15,
    )
    response.raise_for_status()

    data = response.json()
    _access_token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    _token_expires_at = time.time() + expires_in - _TOKEN_EXPIRY_BUFFER

    # Invalidate contacts cache whenever we get a new token
    _contacts_cache = None

    logger.info(f"Hyperion TMS token acquired, expires in {expires_in}s")
    return _access_token


def _get_token() -> str:
    """Return a valid Bearer token, refreshing if expired."""
    if _access_token is None or time.time() >= _token_expires_at:
        return _fetch_token()
    return _access_token


# ---------------------------------------------------------------------------
# Customer contacts
# ---------------------------------------------------------------------------

def fetch_customer_contacts() -> list:
    """
    Fetch the full customer contacts list from Hyperion TMS.
    Result is cached in memory until the token is refreshed.
    """
    global _contacts_cache

    if _contacts_cache is not None:
        return _contacts_cache

    token = _get_token()
    url = "https://3pl.hyperiontms.com/api/clientv1/customercontacts"

    logger.info("Fetching Hyperion TMS customer contacts")
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    response.raise_for_status()

    _contacts_cache = response.json()
    logger.info(f"Fetched {len(_contacts_cache)} customer contacts")
    return _contacts_cache


def get_slim_customer_list() -> list:
    """
    Return deduplicated list of {customerId, customerName} from the contacts API.
    Used to pass a minimal customer reference to OpenAI for ID resolution.
    """
    contacts = fetch_customer_contacts()
    seen = set()
    slim = []
    for c in contacts:
        cid = c.get("customerId")
        name = c.get("customerName")
        if cid and name and cid not in seen:
            seen.add(cid)
            slim.append({"customerId": cid, "customerName": name})
    return slim


def resolve_customer_id(sender_email: Optional[str]) -> Optional[int]:
    """
    Resolve a customerId from the sender's email address.

    Matching strategy (in order):
      1. Exact email match      — most reliable, handles multi-ID domains
      2. Domain-only fallback   — used only when exactly one customer matches
      3. No match               — returns None, customerId stays null in output

    Args:
        sender_email: The From address extracted from the email header.

    Returns:
        Integer customerId if matched, None otherwise.
    """
    if not sender_email:
        return None

    # Handle "Display Name <email@domain.com>" format from email headers
    sender_email = sender_email.strip()
    if "<" in sender_email and ">" in sender_email:
        sender_email = sender_email.split("<", 1)[1].split(">", 1)[0].strip()
    sender_email = sender_email.lower()

    try:
        contacts = fetch_customer_contacts()
    except Exception as e:
        logger.error(f"Failed to fetch customer contacts: {e}")
        return None

    # ── Level 1: exact email match ────────────────────────────────────────
    for contact in contacts:
        contact_email = (contact.get("email") or "").strip().lower()
        if contact_email and contact_email == sender_email:
            customer_id = contact.get("customerId")
            logger.info(f"Exact email match: {sender_email} → customerId {customer_id}")
            return customer_id

    # ── Level 2: domain fallback ─────────────────────────────────────────
    if "@" not in sender_email:
        return None

    domain = sender_email.split("@", 1)[1]
    domain_matches = [
        c for c in contacts
        if (c.get("email") or "").strip().lower().endswith(f"@{domain}")
    ]

    # Only use domain match if all matches point to the same customerId
    customer_ids = list({c.get("customerId") for c in domain_matches if c.get("customerId")})
    if len(customer_ids) == 1:
        logger.info(f"Domain match: {domain} → customerId {customer_ids[0]}")
        return customer_ids[0]

    if len(customer_ids) > 1:
        logger.warning(
            f"Domain '{domain}' maps to multiple customer IDs {customer_ids} — "
            "cannot auto-resolve, returning None"
        )

    return None
