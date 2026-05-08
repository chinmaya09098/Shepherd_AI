"""
Azure AI Search client for customer contact vector search.

Flow:
  1. Contacts are fetched from Hyperion API and indexed with embeddings.
  2. When processing an email:
       a. Broker domain check (hardcoded mappings) → keyword search by customer name → 1 exact result
       b. Unknown/direct sender → vector search by sender domain → top results filtered by score
  3. Results are passed to OpenAI for final customerId confirmation.
"""
import hashlib
from typing import Optional
from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential
from src.config import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded broker domain → customer name mappings
# These brokers send emails on behalf of the mapped customer.
# Customer names must match exactly as stored in the Hyperion TMS / index.
# ---------------------------------------------------------------------------
BROKER_DOMAIN_MAPPINGS: dict[str, str] = {
    "ecogistics.org": "Disney Corporate",
    "kloeckner.com": "ABC - Alen",
    "nucorskyline.com": "Denon",   # not yet in TMS — will return null until added
}

# Minimum vector similarity score to pass a result to OpenAI.
# Results below this threshold are discarded to prevent false matches.
SCORE_THRESHOLD: float = 0.70

# Module-level fingerprint — tracks last indexed contacts state.
_contacts_hash: Optional[str] = None


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def _get_embedding_client() -> AzureOpenAI:
    return AzureOpenAI(
        api_key=Config.AZURE_OPENAI_KEY,
        api_version=Config.AZURE_OPENAI_API_VERSION,
        azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
    )


def _get_search_client() -> SearchClient:
    return SearchClient(
        endpoint=Config.AZURE_SEARCH_ENDPOINT,
        index_name=Config.AZURE_SEARCH_INDEX_NAME,
        credential=AzureKeyCredential(Config.AZURE_SEARCH_KEY),
    )


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed(text: str) -> list:
    """Generate an embedding vector for the given text."""
    client = _get_embedding_client()
    response = client.embeddings.create(
        input=text,
        model=Config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
    )
    return response.data[0].embedding


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _contacts_fingerprint(contacts: list) -> str:
    """MD5 hash of contacts — used to detect when re-indexing is needed."""
    key_data = sorted([
        f"{c.get('customerId')}:{c.get('customerName')}:{c.get('email')}"
        for c in contacts
    ])
    return hashlib.md5("|".join(key_data).encode()).hexdigest()


def index_contacts(contacts: list) -> None:
    """
    Embed each contact and upsert into Azure AI Search.
    Called automatically when contacts are fetched fresh from the Hyperion API.
    """
    global _contacts_hash

    if not contacts:
        logger.warning("No contacts to index")
        return

    if not Config.AZURE_SEARCH_ENDPOINT or not Config.AZURE_SEARCH_KEY:
        logger.warning("Azure AI Search not configured — skipping indexing")
        return

    search_client = _get_search_client()
    documents = []

    for contact in contacts:
        customer_id = contact.get("customerId")
        customer_name = contact.get("customerName") or ""
        email = contact.get("email") or ""
        email_domain = email.split("@")[1].lower() if "@" in email else ""

        # Embed: customerName + emailDomain gives the richest signal for matching
        embed_text = f"{customer_name} {email_domain}".strip()

        try:
            vector = _embed(embed_text)
        except Exception as e:
            logger.error(f"Failed to embed contact '{customer_name}': {e}")
            continue

        documents.append({
            "id": str(customer_id),
            "customerId": customer_id,
            "customerName": customer_name,
            "email": email,
            "emailDomain": email_domain,
            "contentVector": vector,
        })

    if documents:
        search_client.upload_documents(documents=documents)
        logger.info(f"Indexed {len(documents)} contacts into Azure AI Search")
        _contacts_hash = _contacts_fingerprint(contacts)


def _ensure_indexed() -> None:
    """Re-index contacts only if they have changed since last indexing."""
    global _contacts_hash

    from src.services.hyperion_client import fetch_customer_contacts
    try:
        contacts = fetch_customer_contacts()
        current_hash = _contacts_fingerprint(contacts)
        if current_hash != _contacts_hash:
            logger.info("Contacts changed or not yet indexed — re-indexing now")
            index_contacts(contacts)
    except Exception as e:
        logger.error(f"Failed to ensure contacts indexed: {e}")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _search_by_customer_name(customer_name: str) -> list:
    """
    Search for a specific customer name and apply exact match in Python.
    Used for the broker mapping case where we already know the customer name.
    Fetches top 10 keyword results then filters locally for exact name match
    (e.g. "Disney Corporate" must not match "Disney Anaheim").
    Returns at most 1 result.
    """
    search_client = _get_search_client()
    results = search_client.search(
        search_text=customer_name,
        search_fields=["customerName"],
        select=["customerId", "customerName", "email", "emailDomain"],
        top=10,
    )

    # Exact match in Python — case-insensitive, strip whitespace
    target = customer_name.strip().lower()
    matches = [
        {
            "customerId": r["customerId"],
            "customerName": r["customerName"],
            "email": r.get("email", ""),
            "emailDomain": r.get("emailDomain", ""),
        }
        for r in results
        if r.get("customerName", "").strip().lower() == target
    ]

    if matches:
        logger.info(f"Exact name match for '{customer_name}' → found: {matches[0]['customerName']} (id: {matches[0]['customerId']})")
    else:
        logger.warning(f"Exact name match for '{customer_name}' → no results in index")
    return matches[:1]


def _search_by_vector(query_text: str, top: int = 3) -> list:
    """
    Vector search for unknown/direct senders.
    Filters results by SCORE_THRESHOLD before returning.
    """
    query_vector = _embed(query_text)
    search_client = _get_search_client()

    vector_query = VectorizedQuery(
        vector=query_vector,
        k_nearest_neighbors=top,
        fields="contentVector",
    )

    results = search_client.search(
        search_text=None,
        vector_queries=[vector_query],
        select=["customerId", "customerName", "email", "emailDomain"],
        top=top,
    )

    matches = []
    for r in results:
        score = r.get("@search.score", 0)
        if score >= SCORE_THRESHOLD:
            matches.append({
                "customerId": r["customerId"],
                "customerName": r["customerName"],
                "email": r.get("email", ""),
                "emailDomain": r.get("emailDomain", ""),
            })
            logger.info(f"Vector match: {r['customerName']} (id: {r['customerId']}) score={score:.3f}")
        else:
            logger.info(f"Vector result below threshold: {r['customerName']} score={score:.3f} — discarded")

    return matches


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _extract_domain(email: str) -> Optional[str]:
    """Extract domain from an email address, handling 'Display Name <email>' format."""
    if not email:
        return None
    if "<" in email and ">" in email:
        email = email.split("<", 1)[1].split(">", 1)[0].strip()
    email = email.lower().strip()
    if "@" not in email:
        return None
    return email.split("@", 1)[1]


def find_customer_matches(sender_email: str, receiver_email: str = "") -> dict:
    """
    Find the best customer match(es) for a given email.

    Strategy:
      1. Check receiver (To) domain against BROKER_DOMAIN_MAPPINGS first.
         Broker domain is in the To field — the broker receives the load tender email.
         e.g. To: jeff.lohr@ecogistics.org → ecogistics.org → "Disney Corporate"
      2. If receiver is a broker → exact name search in index → 1 result.
         is_broker_match=True — caller uses customerId directly, no OpenAI needed.
      3. If no broker match → vector search using sender (From) domain → top 3 filtered by score.
         is_broker_match=False — caller passes matches to OpenAI for confirmation.

    Returns:
      {
        "matches": [ {customerId, customerName, email, emailDomain}, ... ],
        "is_broker_match": bool
      }
    """
    _ensure_indexed()

    sender_domain = _extract_domain(sender_email)
    receiver_domain = _extract_domain(receiver_email)

    # ── Check both From and To domains for broker mapping ─────────────────
    # Covers two email patterns:
    #   1. Broker forwards email → From: jeff@ecogistics.org (sender is broker)
    #   2. Original email sent to broker → To: jeff@ecogistics.org (receiver is broker)
    broker_domain = None
    if sender_domain and sender_domain in BROKER_DOMAIN_MAPPINGS:
        broker_domain = sender_domain
        logger.info(f"Broker match on sender domain: {sender_domain}")
    elif receiver_domain and receiver_domain in BROKER_DOMAIN_MAPPINGS:
        broker_domain = receiver_domain
        logger.info(f"Broker match on receiver domain: {receiver_domain}")

    if broker_domain:
        customer_name = BROKER_DOMAIN_MAPPINGS[broker_domain]
        logger.info(f"Broker domain '{broker_domain}' → '{customer_name}'")
        matches = _search_by_customer_name(customer_name)
        return {"matches": matches, "is_broker_match": True}

    # ── No broker match — vector search using sender domain ───────────────
    if not sender_domain:
        return {"matches": [], "is_broker_match": False}

    logger.info(f"No broker match — running vector search for sender domain: {sender_domain}")
    matches = _search_by_vector(query_text=sender_domain, top=3)
    return {"matches": matches, "is_broker_match": False}
