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


def sync_from_brokerware(default_retry_count: Optional[int] = None) -> int:
    """Fetch CustomerContactsSummary from Brokerware and seed customer_retry_config.

    For each unique (clientId, customerId) pair returned by the API a row is
    upserted.  Existing rows where an admin has already edited retry_count are
    NOT overwritten (see upsert_retry_configs).

    Args:
        default_retry_count: Override the default retry count for new rows.
                             Defaults to Config.FOLLOWUP_DEFAULT_MAX.

    Returns the number of rows upserted (0 on error or empty API response).
    """
    from src.services.brokerware_client import get_customer_contacts

    default = default_retry_count if default_retry_count is not None else Config.FOLLOWUP_DEFAULT_MAX
    domain  = _brokerware_domain()

    contacts = get_customer_contacts()
    if not contacts:
        logger.warning("sync_from_brokerware: no contacts returned from Brokerware API")
        return 0

    # De-duplicate by customerId (multiple emails can share one customerId)
    seen: set = set()
    rows: List[dict] = []
    for c in contacts:
        cust_id   = c.get("customerId")
        client_id = c.get("clientId")
        if cust_id is None or cust_id in seen:
            continue
        seen.add(cust_id)
        rows.append({
            "customer_id": int(cust_id),
            "client_id":   int(client_id) if client_id is not None else None,
            "domain":      domain,
            "retry_count": default,
        })

    logger.info(
        "sync_from_brokerware: %d unique customer(s) found in Brokerware contacts "
        "(domain=%s, default_retry=%d)",
        len(rows), domain, default,
    )
    return upsert_retry_configs(rows)
