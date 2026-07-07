"""
Conversation lifecycle logger.

Writes structured JSON event records to Azure Blob Storage as an external
audit trail (separate from the inline ConversationState.lifecycle_events list).

Blob path: lifecycle-logs/<conversation_id>.jsonl
Each line is a self-contained JSON record:
  {
    "conversation_id": "...",
    "event":           "followup_sent",
    "timestamp":       "2026-03-05T14:32:00+00:00",
    "details":         { "missing_fields": ["pickupDate"], "followup_count": 1 }
  }

Falls back to a local .jsonl file under OUTPUT_DIR/lifecycle_logs/ when
blob storage is not configured.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

_BLOB_PREFIX = "lifecycle-logs/"
_LOCAL_DIR = (
    Path(Config.OUTPUT_DIR if hasattr(Config, "OUTPUT_DIR") else "output")
    / "lifecycle_logs"
)


class ConversationLifecycleLogger:
    """
    Appends lifecycle events to a per-conversation JSONL log in Azure Blob Storage.

    Usage::

        lifecycle = ConversationLifecycleLogger()
        lifecycle.log(
            conversation_id="AAQ...",
            event="followup_sent",
            details={"missing_fields": ["pickupDate"], "followup_count": 1},
        )
    """

    def log(
        self,
        conversation_id: str,
        event: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Append one event record to the conversation's JSONL log.

        Args:
            conversation_id: The Graph conversationId (or any stable thread key).
            event:           Event name (use the EVENT_* constants from conversation_state).
            details:         Optional dict of event-specific metadata.
        """
        record = {
            "conversation_id": conversation_id,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "details": details or {},
        }
        line = json.dumps(record, default=str) + "\n"

        # ── Azure Blob Storage (append pattern via download → append → upload) ──
        blob_client = self._get_blob_client(conversation_id)
        if blob_client:
            try:
                try:
                    existing = blob_client.download_blob().readall().decode("utf-8")
                except Exception:
                    existing = ""
                blob_client.upload_blob(existing + line, overwrite=True)
                logger.debug(
                    "Lifecycle event logged to blob: conv=%s event=%s",
                    conversation_id[:20],
                    event,
                )
                return
            except Exception as exc:
                logger.warning("Blob lifecycle log failed, using local fallback: %s", exc)

        # ── Local fallback ────────────────────────────────────────────────────
        try:
            _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            safe = _safe_filename(conversation_id)
            path = _LOCAL_DIR / f"{safe}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
            logger.debug("Lifecycle event logged locally: %s event=%s", path, event)
        except Exception as exc:
            logger.error("Failed to write lifecycle event locally: %s", exc)

    def read_events(self, conversation_id: str) -> List[Dict[str, Any]]:
        """
        Read all logged events for a conversation (for UI display / audit).

        Returns:
            List of event dicts in chronological order.
        """
        events: List[Dict[str, Any]] = []

        blob_client = self._get_blob_client(conversation_id)
        if blob_client:
            try:
                raw = blob_client.download_blob().readall().decode("utf-8")
                for line in raw.splitlines():
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                return events
            except Exception:
                pass  # Fall through to local

        safe = _safe_filename(conversation_id)
        path = _LOCAL_DIR / f"{safe}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        return events

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_blob_client(conversation_id: str):
        if not Config.AZURE_STORAGE_CONNECTION_STRING:
            return None
        try:
            from azure.storage.blob import BlobServiceClient

            service = BlobServiceClient.from_connection_string(
                Config.AZURE_STORAGE_CONNECTION_STRING
            )
            container = Config.AZURE_STORAGE_LOGS_CONTAINER or "processed-logs"
            blob_name = f"{_BLOB_PREFIX}{_safe_filename(conversation_id)}.jsonl"
            return service.get_blob_client(container=container, blob=blob_name)
        except Exception as exc:
            logger.debug("Could not create lifecycle blob client: %s", exc)
            return None


def _safe_filename(conversation_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in conversation_id)
