"""
Human-in-the-Loop (HITL) review queue — persistence layer.

Storage mirrors ConversationTracker:
  Primary  : Azure Blob Storage  ({scope}/review-queue/<review_id>.json)
  Fallback : Local JSON files    (OUTPUT_DIR/review_queue/<review_id>.json)
  Cache    : Module-level dict   (survives Streamlit reruns in one process)

Blob path scoping:
  When TENANT_ID_SCOPE is set (or req.tenant_id is non-None), blobs are
  stored under "{scope}/review-queue/".  An empty scope uses the legacy
  unscoped path "review-queue/" for backward compatibility.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config
from src.models.review_request import ReviewRequest, REVIEW_STATUS_PENDING
from src.utils.logger import get_logger

logger = get_logger(__name__)

_LOCAL_DIR = (
    Path(Config.OUTPUT_DIR if hasattr(Config, "OUTPUT_DIR") else "output")
    / "review_queue"
)


def _review_blob_prefix(tenant_id: Optional[str] = None) -> str:
    """Return the blob path prefix for review queue, optionally scoped to a tenant.

    An empty scope (default) uses the legacy unscoped "review-queue/" path so
    existing blobs are unaffected when TENANT_ID_SCOPE is first introduced.
    """
    scope = tenant_id or Config.TENANT_ID_SCOPE
    if scope:
        return f"{scope}/review-queue/"
    return "review-queue/"


class ReviewQueue:
    """
    Persists and retrieves ReviewRequest objects.

    Module-level cache survives Streamlit reruns within one process.
    All writes update the cache immediately; reads check cache first.
    """

    _cache: Dict[str, ReviewRequest] = {}

    # ── Write ─────────────────────────────────────────────────────────────────

    def submit(self, req: ReviewRequest) -> bool:
        """
        Persist a new ReviewRequest.

        Returns True on success.
        """
        self._cache[req.review_id] = req

        blob_client = self._get_blob_client(req.review_id, tenant_id=req.tenant_id)
        if blob_client:
            try:
                blob_client.upload_blob(req.model_dump_json(indent=2), overwrite=True)
                logger.info(
                    "ReviewRequest submitted: id=%s reason=%s",
                    req.review_id[:8], req.review_reason,
                )
                return True
            except Exception as exc:
                logger.warning("Blob submit failed, using local fallback: %s", exc)

        try:
            _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            path = _LOCAL_DIR / f"{req.review_id}.json"
            path.write_text(req.model_dump_json(indent=2), encoding="utf-8")
            logger.info("ReviewRequest saved locally: %s", path.name)
            return True
        except Exception as exc:
            logger.error("Failed to save ReviewRequest locally: %s", exc)
            return False

    def save(self, req: ReviewRequest) -> bool:
        """Persist an existing ReviewRequest (status update, approval, etc.)."""
        return self.submit(req)

    # ── Read ──────────────────────────────────────────────────────────────────

    def load(self, review_id: str) -> Optional[ReviewRequest]:
        """Load a ReviewRequest by ID. Checks cache → blob (scoped then legacy) → local disk."""
        if review_id in self._cache:
            return self._cache[review_id]

        # Try scoped path then legacy unscoped path (backward compat)
        for tenant_scope in [None, ""]:
            blob_client = self._get_blob_client(review_id, tenant_id=tenant_scope)
            if blob_client:
                try:
                    data = blob_client.download_blob().readall()
                    req  = ReviewRequest.model_validate_json(data)
                    self._cache[review_id] = req
                    return req
                except Exception:
                    pass
            if not Config.TENANT_ID_SCOPE:
                break   # scoped == legacy when scope is empty; no need to try twice

        path = _LOCAL_DIR / f"{review_id}.json"
        if path.exists():
            try:
                req = ReviewRequest.model_validate_json(path.read_text(encoding="utf-8"))
                self._cache[review_id] = req
                return req
            except Exception as exc:
                logger.error("Failed to load ReviewRequest %s: %s", review_id, exc)

        return None

    def get_pending(self) -> List[ReviewRequest]:
        """Return all ReviewRequests with status == 'pending', newest first."""
        return sorted(
            [r for r in self._all() if r.status == REVIEW_STATUS_PENDING],
            key=lambda r: r.created_at,
            reverse=True,
        )

    def list_all(self) -> List[ReviewRequest]:
        """Return all ReviewRequests regardless of status, newest first."""
        return sorted(self._all(), key=lambda r: r.created_at, reverse=True)

    # ── Status updates ────────────────────────────────────────────────────────

    def approve(
        self,
        review_id: str,
        approved_shipment: dict,
        reviewer_notes: str = "",
    ) -> bool:
        """
        Approve a review with the reviewer-verified shipment data.

        Args:
            review_id:         ID of the ReviewRequest to approve.
            approved_shipment: Final shipment dict (reviewer may have edited fields).
            reviewer_notes:    Optional free-text notes from the reviewer.

        Returns:
            True on success, False if review not found.
        """
        req = self.load(review_id)
        if not req:
            logger.error("ReviewRequest %s not found — cannot approve", review_id)
            return False

        req.approve(approved_shipment, reviewer_notes)
        saved = self.save(req)
        if saved:
            logger.info("ReviewRequest %s approved", review_id[:8])
        return saved

    def reject(self, review_id: str, reviewer_notes: str = "") -> bool:
        """
        Reject a review.

        Returns:
            True on success, False if review not found.
        """
        req = self.load(review_id)
        if not req:
            logger.error("ReviewRequest %s not found — cannot reject", review_id)
            return False

        req.reject(reviewer_notes)
        saved = self.save(req)
        if saved:
            logger.info("ReviewRequest %s rejected", review_id[:8])
        return saved

    # ── Internal ──────────────────────────────────────────────────────────────

    def _all(self) -> List[ReviewRequest]:
        """Collect all reviews from cache + local disk (deduplicated)."""
        seen: set = set(self._cache.keys())
        all_reviews = list(self._cache.values())

        if _LOCAL_DIR.exists():
            for json_file in _LOCAL_DIR.glob("*.json"):
                review_id = json_file.stem
                if review_id not in seen:
                    try:
                        req = ReviewRequest.model_validate_json(
                            json_file.read_text(encoding="utf-8")
                        )
                        all_reviews.append(req)
                        seen.add(review_id)
                        self._cache[review_id] = req
                    except Exception:
                        pass

        return all_reviews

    @staticmethod
    def _get_blob_client(review_id: str, tenant_id: Optional[str] = None):
        """Return a BlobClient for the review queue blob.

        Args:
            review_id:  Unique review ID (used as filename).
            tenant_id:  Override tenant scope. None → Config.TENANT_ID_SCOPE;
                        "" → force legacy unscoped path.
        """
        if not Config.AZURE_STORAGE_CONNECTION_STRING:
            return None
        try:
            from azure.storage.blob import BlobServiceClient
            service   = BlobServiceClient.from_connection_string(
                Config.AZURE_STORAGE_CONNECTION_STRING
            )
            container = Config.AZURE_STORAGE_LOGS_CONTAINER or "processed-logs"
            blob_name = f"{_review_blob_prefix(tenant_id)}{review_id}.json"
            return service.get_blob_client(container=container, blob=blob_name)
        except Exception as exc:
            logger.debug("Could not create review blob client: %s", exc)
            return None
