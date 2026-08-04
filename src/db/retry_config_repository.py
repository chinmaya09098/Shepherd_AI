"""
Customer retry-config repository — read/write for the customer_retry_config table.

Responsibilities:
  - get_retry_count(customer_id)  : return max follow-ups for a customer (DB → default).
  - upsert_retry_configs(rows)    : bulk-upsert a list of config dicts into the table.
  - sync_from_brokerware()        : pull CustomerContactsSummary and seed the table
                                    for any customer not yet present (preserves
                                    existing retry_count values set by admins).

The table is auto-created by init_db() via Base.metadata.create_all().
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlparse

from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import Config
from src.db.database import get_session, init_db
from src.db.models import CustomerRetryConfig
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _brokerware_domain() -> str:
    """Extract the hostname from BROKERWARE_BASE_URL (e.g. 'shepherd.brokerware.io')."""
    raw = Config.BROKERWARE_BASE_URL or ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return parsed.hostname or raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_retry_count(customer_id: Optional[int]) -> int:
    """Return the configured max follow-up count for *customer_id*.

    Lookup order:
      1. customer_retry_config table row for this customer_id
      2. Config.FOLLOWUP_DEFAULT_MAX (env var, default 3)

    Never raises — falls back to the default on any DB error.
    """
    default = Config.FOLLOWUP_DEFAULT_MAX
    if customer_id is None:
        return default

    if not init_db():
        return default

    try:
        with get_session() as session:
            row = session.get(CustomerRetryConfig, int(customer_id))
            if row is not None:
                return row.retry_count
    except Exception as exc:
        logger.warning(
            "get_retry_count: DB error for customer_id=%s — using default %d. Error: %s",
            customer_id, default, exc,
        )
    return default


def upsert_retry_configs(rows: List[dict]) -> int:
    """Bulk-upsert a list of retry-config dicts into customer_retry_config.

    Each dict must have:
        customer_id  (int)  — primary key
        client_id    (int)  — Brokerware clientId
        domain       (str)  — Brokerware subdomain
        retry_count  (int)  — max follow-ups (optional, defaults to FOLLOWUP_DEFAULT_MAX)

    On conflict (customer_id already exists):
        - client_id and domain are always updated (may change if Brokerware data changes)
        - retry_count is NOT overwritten so admin edits are preserved

    Returns the number of rows upserted.
    """
    if not rows:
        return 0
    if not init_db():
        logger.warning("upsert_retry_configs: DB not available — skipping")
        return 0

    default_retries = Config.FOLLOWUP_DEFAULT_MAX
    try:
        with get_session() as session:
            stmt = pg_insert(CustomerRetryConfig).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["customer_id"],
                set_={
                    "client_id":  stmt.excluded.client_id,
                    "domain":     stmt.excluded.domain,
                    # Preserve admin-set retry_count — only update when the incoming
                    # value differs from the table default so explicit admin edits
                    # are never silently overwritten by a sync run.
                    "retry_count": stmt.excluded.retry_count,
                },
                where=(CustomerRetryConfig.retry_count == default_retries),
            )
            result = session.execute(stmt)
            session.commit()
            count = result.rowcount
            logger.info("upsert_retry_configs: %d row(s) upserted", count)
            return count
    except Exception as exc:
        logger.error("upsert_retry_configs failed: %s", exc, exc_info=True)
        return 0


def ensure_customer_exists(
    customer_id: int,
    client_id: Optional[int],
    domain: str,
) -> bool:
    """Insert a row for *customer_id* only if it does not already exist.

    Uses INSERT … ON CONFLICT DO NOTHING so existing rows (including any
    admin-edited retry_count) are never touched.  This is the hot-path
    companion to sync_from_brokerware() — called on every successful email
    match so new customers are auto-seeded without a manual sync.

    Returns True if the row was inserted (new customer), False if it already
    existed or if the DB is unavailable.
    """
    if not init_db():
        return False

    default_retries = Config.FOLLOWUP_DEFAULT_MAX
    try:
        with get_session() as session:
            stmt = pg_insert(CustomerRetryConfig).values([{
                "customer_id": int(customer_id),
                "client_id":   client_id,
                "domain":      domain,
                "retry_count": default_retries,
            }]).on_conflict_do_nothing(index_elements=["customer_id"])
            result = session.execute(stmt)
            session.commit()
            inserted = bool(result.rowcount)
            if inserted:
                logger.info(
                    "ensure_customer_exists: auto-seeded customer_id=%s domain=%s",
                    customer_id, domain,
                )
            return inserted
    except Exception as exc:
        logger.warning(
            "ensure_customer_exists: DB error for customer_id=%s — %s",
            customer_id, exc,
        )
        return False


def sync_from_brokerware(default_retry_count: Optional[int] = None) -> int:
    """Fetch CustomerContactsSummary from every configured Brokerware tenant and
    seed customer_retry_config.

    Iterates over all unique tenant keys in BROKERWARE_MAILBOX_TENANT_MAP and
    calls CustomerContactsSummary once per tenant.  Falls back to the default
    tenant when the map is empty.  Existing rows where an admin has already
    edited retry_count are NOT overwritten (see upsert_retry_configs).

    This covers the multi-tenant case described by Jack:
      - Shepherd tenant  (shepherd@3pl, shepherd1@3pl)  → shepherd.brokerware.io
      - Shepherd West    (shepherd2@3pl)                 → shepherdwest.brokerware.io
    Each tenant's contacts are stored with their own domain and customerIds.
    Since Brokerware assigns different customerIds per tenant, there is no
    collision even when the same email (e.g. dan@amazon) exists in both.

    Args:
        default_retry_count: Override the default retry count for new rows.
                             Defaults to Config.FOLLOWUP_DEFAULT_MAX.

    Returns the total number of rows upserted across all tenants.
    """
    import json as _json
    from src.services.brokerware_client import get_customer_contacts, _load_tenant

    default = default_retry_count if default_retry_count is not None else Config.FOLLOWUP_DEFAULT_MAX

    # Build one representative mailbox UPN per unique tenant key so we can
    # call get_customer_contacts with the right credentials for each tenant.
    try:
        tenant_map: dict = _json.loads(Config.BROKERWARE_MAILBOX_TENANT_MAP or "{}")
    except Exception:
        tenant_map = {}

    # {tenant_key: first_upn_for_that_key}  — one UPN per tenant is enough
    key_to_upn: dict = {}
    for upn, key in tenant_map.items():
        if key not in key_to_upn:
            key_to_upn[key] = upn

    # If no tenant map configured, fall back to the default/legacy single tenant
    upns_to_sync: List[str] = list(key_to_upn.values()) or [""]

    all_rows: List[dict] = []
    seen_customer_ids: set = set()

    for upn in upns_to_sync:
        tenant = _load_tenant(upn)
        raw_url = tenant.base_url or ""
        parsed  = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
        domain  = parsed.hostname or raw_url

        contacts = get_customer_contacts(mailbox_upn=upn)
        if not contacts:
            logger.warning(
                "sync_from_brokerware: no contacts returned for tenant=%s (upn=%s)",
                tenant.key, upn or "(default)",
            )
            continue

        for c in contacts:
            cust_id   = c.get("customerId")
            client_id = c.get("clientId")
            if cust_id is None or cust_id in seen_customer_ids:
                continue
            seen_customer_ids.add(cust_id)
            all_rows.append({
                "customer_id": int(cust_id),
                "client_id":   int(client_id) if client_id is not None else None,
                "domain":      domain,
                "retry_count": default,
            })

        logger.info(
            "sync_from_brokerware: tenant=%s fetched %d contact(s) from %s",
            tenant.key, len(contacts), domain,
        )

    if not all_rows:
        logger.warning("sync_from_brokerware: no contacts found across any configured tenant")
        return 0

    logger.info(
        "sync_from_brokerware: %d unique customer(s) total across %d tenant(s) "
        "(default_retry=%d)",
        len(all_rows), len(upns_to_sync), default,
    )
    return upsert_retry_configs(all_rows)
