"""
In-memory log handler that captures log records during processing
and uploads them to Azure Blob Storage when flushed.
"""
import logging
import io
from datetime import datetime, timezone
from typing import Optional


class BlobLogHandler(logging.Handler):
    """
    Captures log records into an in-memory buffer.
    Call upload() to push the buffer to Azure Blob Storage.
    """

    def __init__(self):
        super().__init__()
        self._buffer = io.StringIO()
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord):
        try:
            self._buffer.write(self.format(record) + '\n')
        except Exception:
            self.handleError(record)

    def get_logs(self) -> str:
        return self._buffer.getvalue()

    def upload(self, blob_service_client, container: str, eml_filename: str) -> Optional[str]:
        """
        Upload captured logs to:
          <container>/Processed_Logs/<eml_filename_stem>_<timestamp>.log

        Returns the blob path on success, None on failure.
        """
        if not blob_service_client or not container:
            return None

        logs = self.get_logs()
        if not logs:
            return None

        try:
            from pathlib import Path
            stem = Path(eml_filename).stem.replace(" ", "_")
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            blob_path = f"Processed_Logs/{stem}_{timestamp}.log"

            container_client = blob_service_client.get_container_client(container)
            blob_client = container_client.get_blob_client(blob_path)
            blob_client.upload_blob(logs.encode("utf-8"), overwrite=True)
            return blob_path
        except Exception as e:
            # Don't let log upload failure break anything
            import logging as _log
            _log.getLogger(__name__).error(f"Failed to upload processing log: {e}")
            return None
