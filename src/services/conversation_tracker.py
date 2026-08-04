"""
Conversation tracker — persists, retrieves, and correlates ConversationState objects.

Storage:
  Primary  : Azure Blob Storage  ({scope}/conversation-states/<id>.json)
  Fallback : Local JSON files    (OUTPUT_DIR/conversation_states/<id>.json)
  Cache    : Module-level dict   (survives Streamlit reruns within one process)

Blob path scoping:
  When TENANT_ID_SCOPE is set (or state.tenant_id is non-None), blobs are stored
  under "{scope}/conversation-states/".  When blank (default), the legacy
  unscoped path "conversation-states/" is used for backward compatibility.
  load() also performs a legacy fallback so existing blobs are found after a
  TENANT_ID_SCOPE is first introduced.

Correlation strategies (in priority order):
  1. conversation_id  — exact Graph conversationId match          (confidence 1.0)
  2. reference_id     — shared shipment/order/PO reference number  (confidence 0.9)
  3. subject          — normalised subject-line match              (confidence 0.80–0.85)
  4. sender           — same sender email, single open thread      (confidence 0.6–0.75)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config
from src.models.conversation_state import ConversationState
from src.models.graph_models import MailMessage
from src.utils.logger import get_logger

logger = get_logger(__name__)

_LOCAL_DIR = (
    Path(Config.OUTPUT_DIR if hasattr(Config, "OUTPUT_DIR") else "output")
    / "conversation_states"
)


def _blob_prefix(tenant_id: Optional[str] = None) -> str:
    """Return the blob path prefix, optionally scoped to a tenant.

    When TENANT_ID_SCOPE is configured (or a non-None tenant_id is supplied),
    blobs are stored under "{scope}/conversation-states/" so multiple tenants
    share one storage container without collisions.  An empty scope uses the
    legacy unscoped "conversation-states/" path for backward compatibility.
    """
    scope = tenant_id or Config.TENANT_ID_SCOPE
    if scope:
        return f"{scope}/conversation-states/"
    return "conversation-states/"


# ---------------------------------------------------------------------------
# Correlation result
# ---------------------------------------------------------------------------

@dataclass
class CorrelationResult:
    """
    Describes how (and how confidently) an incoming message was matched to
    an existing tracked conversation.

    Attributes:
        state:      The matched ConversationState, or None.
        method:     How the match was made:
                        "conversation_id"  — exact Graph thread ID (most reliable)
                        "reference_id"     — shared shipment/order/PO number
                        "subject"          — normalised subject-line match
                        "sender+subject"   — sender match narrowed by subject
                        "sender"           — same sender, only one open thread
                        "none"             — no match found
        confidence: 0.0–1.0 indicating match reliability.
        is_reply:   True when state is set and message is a genuine customer reply
                    (not a re-selection of the original message).
    """
    state:      Optional[ConversationState] = None
    method:     str                         = "none"
    confidence: float                       = 0.0
    is_reply:   bool                        = False


# ---------------------------------------------------------------------------
# Subject normalisation helper
# ---------------------------------------------------------------------------

_SUBJECT_PREFIX_RE = re.compile(
    r"^(\[.*?\]\s*)*"                       # [External] tags
    r"(re|fw|fwd|tr|aw|r|sv|re\[\d+\])\s*:\s*",  # Re:/Fw: prefixes
    re.IGNORECASE,
)


def _normalize_subject(subject: str) -> str:
    """
    Strip reply / forward prefixes and normalise whitespace.

    Examples:
        "Re: Re: [External] Load Tender #1234"  →  "load tender #1234"
        "Fw: Shipment Request"                  →  "shipment request"
        "FW: Re: Rate Quote"                    →  "rate quote"
    """
    s = subject.strip()
    while True:
        prev = s
        s = _SUBJECT_PREFIX_RE.sub("", s).strip()
        if s == prev:
            break
    return s.lower().strip()


# ---------------------------------------------------------------------------
# ConversationTracker
# ---------------------------------------------------------------------------

class ConversationTracker:
    """
    Manages persistence and correlation of ConversationState objects.

    Module-level cache:
        _cache survives Streamlit reruns within the same Python process.
        All writes update the cache immediately; reads check cache first.
    """

    _cache: Dict[str, ConversationState] = {}

    # ── Write ────────────────────────────────────────────────────────────

    def save(self, state: ConversationState) -> bool:
        """
        Persist a ConversationState to blob storage (or local disk fallback).

        Returns True on success.
        """
        self._cache[state.conversation_id] = state

        blob_client = self._get_blob_client(state.conversation_id, tenant_id=state.tenant_id)
        if blob_client:
            try:
                blob_client.upload_blob(state.model_dump_json(indent=2), overwrite=True)
                logger.info(
                    "ConversationState saved: conv_id=%s status=%s",
                    state.conversation_id[:20],
                    state.status,
                )
                return True
            except Exception as exc:
                logger.warning("Blob save failed, falling back to local: %s", exc)

        try:
            _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            path = _LOCAL_DIR / f"{_safe_filename(state.conversation_id)}.json"
            path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
            logger.info("ConversationState saved locally: %s (status=%s)", path.name, state.status)
            return True
        except Exception as exc:
            logger.error("Failed to save ConversationState locally: %s", exc)
            return False

    # ── Read ─────────────────────────────────────────────────────────────

    def load(self, conversation_id: str) -> Optional[ConversationState]:
        """Load by exact conversation_id. Checks cache → blob (scoped then legacy) → local disk."""
        if conversation_id in self._cache:
            return self._cache[conversation_id]

        # Try tenant-scoped blob first, then fall back to the legacy unscoped path.
        # This ensures existing blobs written before TENANT_ID_SCOPE was introduced
        # are still found after the scope is first set.
        for tenant_scope in [None, ""]:  # None → uses Config.TENANT_ID_SCOPE; "" → legacy
            blob_client = self._get_blob_client(conversation_id, tenant_id=tenant_scope)
            if blob_client:
                try:
                    data  = blob_client.download_blob().readall()
                    state = ConversationState.model_validate_json(data)
                    self._cache[conversation_id] = state
                    return state
                except Exception:
                    pass
            # If scoped == legacy path (TENANT_ID_SCOPE is already ""), don't try twice
            if not Config.TENANT_ID_SCOPE:
                break

        path = _LOCAL_DIR / f"{_safe_filename(conversation_id)}.json"
        if path.exists():
            try:
                state = ConversationState.model_validate_json(path.read_text(encoding="utf-8"))
                self._cache[conversation_id] = state
                return state
            except Exception as exc:
                logger.error("Failed to load ConversationState from %s: %s", path, exc)

        return None

    def is_tracked(self, conversation_id: Optional[str]) -> bool:
        """Return True if any state (any status) exists for this conversation_id."""
        if not conversation_id:
            return False
        return self.load(conversation_id) is not None

    def get_active_state(self, conversation_id: Optional[str]) -> Optional[ConversationState]:
        """Return state only if status == 'awaiting_reply', else None."""
        if not conversation_id:
            return None
        state = self.load(conversation_id)
        return state if (state and state.status == "awaiting_reply") else None

    def list_active(self) -> List[ConversationState]:
        """Return all conversations currently in 'awaiting_reply' status.

        Sources (in order, deduplicating by conversation_id):
          1. In-memory cache (fastest — same process)
          2. Azure Blob Storage (production — across process restarts / scheduler runs)
          3. Local disk (development fallback / offline)
        """
        active:   List[ConversationState] = []
        seen_ids: set                     = set()

        # 1. In-memory cache first
        for state in list(self._cache.values()):
            if state.status == "awaiting_reply":
                active.append(state)
                seen_ids.add(state.conversation_id)

        # 2. Azure Blob Storage — essential when running as a fresh scheduler process
        if Config.AZURE_STORAGE_CONNECTION_STRING:
            try:
                from azure.storage.blob import BlobServiceClient
                service   = BlobServiceClient.from_connection_string(Config.AZURE_STORAGE_CONNECTION_STRING)
                container = Config.AZURE_STORAGE_LOGS_CONTAINER or "processed-logs"
                prefix    = _blob_prefix()
                cc        = service.get_container_client(container)
                for blob_props in cc.list_blobs(name_starts_with=prefix):
                    if not blob_props.name.endswith(".json"):
                        continue
                    try:
                        data  = cc.get_blob_client(blob_props.name).download_blob().readall()
                        state = ConversationState.model_validate_json(data)
                        if (
                            state.status == "awaiting_reply"
                            and state.conversation_id not in seen_ids
                        ):
                            active.append(state)
                            seen_ids.add(state.conversation_id)
                            self._cache[state.conversation_id] = state
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("list_active: blob enumeration failed — %s", exc)

        # 3. Local disk (captures states from previous sessions / dev fallback)
        if _LOCAL_DIR.exists():
            for json_file in _LOCAL_DIR.glob("*.json"):
                try:
                    state = ConversationState.model_validate_json(
                        json_file.read_text(encoding="utf-8")
                    )
                    if state.status == "awaiting_reply" and state.conversation_id not in seen_ids:
                        active.append(state)
                        seen_ids.add(state.conversation_id)
                        self._cache[state.conversation_id] = state
                except Exception:
                    pass

        return active

    def list_stale(self, threshold_hours: int = 24) -> List[ConversationState]:
        """Return awaiting_reply conversations that have not been updated within
        threshold_hours and are still below their max_followups limit.

        Used by the reminder timer trigger to decide which threads need another
        nudge email sent to the customer.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=threshold_hours)
        stale: List[ConversationState] = []
        for state in self.list_active():
            # Skip conversations that already hit their retry ceiling
            if state.followup_count >= state.max_followups:
                continue
            # Use last_followup_at if set, otherwise fall back to when the
            # conversation was created (i.e. the original follow-up was never sent)
            last_activity_str = state.last_followup_at or state.created_at
            try:
                last_dt = datetime.fromisoformat(last_activity_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except Exception:
                # Unparseable timestamp — treat as stale to be safe
                stale.append(state)
                continue
            if last_dt < cutoff:
                stale.append(state)
        return stale

    # ── Fallback correlation ──────────────────────────────────────────────

    def correlate_message(
        self,
        message: MailMessage,
        reference_ids: Optional[List[str]] = None,
    ) -> CorrelationResult:
        """
        Try to match an incoming message to a tracked active conversation.

        Tries four strategies in descending confidence order and returns the
        first successful match.

        Args:
            message:       The incoming MailMessage to correlate.
            reference_ids: Additional reference IDs extracted from the email
                           (shipment_id, order_number, purchase_order, etc.)
                           that supplement those already on the message itself.

        Returns:
            CorrelationResult with the matched state and match metadata.
        """
        ref_ids    = list(reference_ids or [])
        msg_id     = message.id or ""
        conv_id    = message.conversation_id or ""
        sender_raw = ""
        sender_dom = ""
        if message.from_ and message.from_.email_address:
            sender_raw = (message.from_.email_address.address or "").lower()
            sender_dom = sender_raw.split("@")[-1] if "@" in sender_raw else ""
        subject_norm = _normalize_subject(message.subject or "")

        # ── Strategy 1: exact conversationId ─────────────────────────────
        if conv_id:
            state = self.get_active_state(conv_id)
            if state:
                is_reply = (msg_id != state.original_message_id)
                logger.info(
                    "Correlation hit [conversation_id] conv=%s is_reply=%s",
                    conv_id[:20],
                    is_reply,
                )
                return CorrelationResult(
                    state=state,
                    method="conversation_id",
                    confidence=1.0,
                    is_reply=is_reply,
                )

        # Load all active conversations once for the remaining strategies
        active = self.list_active()
        if not active:
            return CorrelationResult()

        # ── Strategy 2: reference ID ──────────────────────────────────────
        if ref_ids:
            for state in active:
                overlap = set(ref_ids) & set(state.reference_ids)
                if overlap:
                    logger.info(
                        "Correlation hit [reference_id] refs=%s conv=%s",
                        overlap,
                        state.conversation_id[:20],
                    )
                    return CorrelationResult(
                        state=state,
                        method="reference_id",
                        confidence=0.9,
                        is_reply=True,
                    )

        # ── Strategy 3: normalised subject ────────────────────────────────
        if subject_norm:
            subject_matches = [
                s for s in active
                if s.normalized_subject and s.normalized_subject == subject_norm
            ]
            if subject_matches:
                # Prefer a sender-domain match among subject hits
                domain_hits = [s for s in subject_matches if s.sender_domain == sender_dom and sender_dom]
                best = domain_hits[0] if domain_hits else subject_matches[0]
                confidence = 0.85 if domain_hits else 0.80
                logger.info(
                    "Correlation hit [subject] subject=%r conv=%s confidence=%.2f",
                    subject_norm[:40],
                    best.conversation_id[:20],
                    confidence,
                )
                return CorrelationResult(
                    state=best,
                    method="subject",
                    confidence=confidence,
                    is_reply=True,
                )

        # ── Strategy 4: sender email ──────────────────────────────────────
        if sender_raw:
            sender_matches = [
                s for s in active
                if s.sender_email.lower() == sender_raw
            ]
            if len(sender_matches) == 1:
                logger.info(
                    "Correlation hit [sender] sender=%s conv=%s",
                    sender_raw,
                    sender_matches[0].conversation_id[:20],
                )
                return CorrelationResult(
                    state=sender_matches[0],
                    method="sender",
                    confidence=0.6,
                    is_reply=True,
                )

            if len(sender_matches) > 1 and subject_norm:
                # Tiebreak with partial subject match
                for s in sender_matches:
                    if s.normalized_subject and (
                        subject_norm in s.normalized_subject
                        or s.normalized_subject in subject_norm
                    ):
                        logger.info(
                            "Correlation hit [sender+subject] sender=%s conv=%s",
                            sender_raw,
                            s.conversation_id[:20],
                        )
                        return CorrelationResult(
                            state=s,
                            method="sender+subject",
                            confidence=0.75,
                            is_reply=True,
                        )

        return CorrelationResult()  # no match

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _get_blob_client(conversation_id: str, tenant_id: Optional[str] = None):
        """Return a BlobClient for the conversation state blob.

        Args:
            conversation_id: The conversation's unique ID (used as filename).
            tenant_id:       Override tenant scope. Pass None to use
                             Config.TENANT_ID_SCOPE; pass "" to force the
                             legacy unscoped path.
        """
        if not Config.AZURE_STORAGE_CONNECTION_STRING:
            return None
        try:
            from azure.storage.blob import BlobServiceClient
            service   = BlobServiceClient.from_connection_string(Config.AZURE_STORAGE_CONNECTION_STRING)
            container = Config.AZURE_STORAGE_LOGS_CONTAINER or "processed-logs"
            blob_name = f"{_blob_prefix(tenant_id)}{_safe_filename(conversation_id)}.json"
            return service.get_blob_client(container=container, blob=blob_name)
        except Exception as exc:
            logger.debug("Could not create blob client: %s", exc)
            return None


def _safe_filename(conversation_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in conversation_id)
