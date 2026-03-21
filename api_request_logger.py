from datetime import datetime
import threading
from typing import List
from pathlib import Path
import time
import json

from utils import ensure_dirs_exist


LOG_FILENAME = "api_logfile.log"


class LogRecord:
    """Represents a single API request log entry."""

    def __init__(self, record: dict) -> None:
        """
        Args:
            record: Raw dict with keys 'uri', 'status', and 'timestamp'.
        """
        self._record_dict = record
        self.timestamp: float = record["timestamp"]
        self.data: dict = {"uri": record["uri"], "status": int(record["status"])}

    def as_dict(self) -> dict:
        """Return the original raw record as a dict (for serialization)."""
        return self._record_dict

    def __repr__(self) -> str:
        ts_str = datetime.fromtimestamp(self.timestamp).strftime(
            "%d-%m-%Y %I:%M:%S,%f %p"
        )
        return f"{ts_str} - {self.data!r}"


class RequestLogger:
    """
    Persists API request logs to a JSON file and provides
    thread-safe read/write access.
    """

    def __init__(self, filepath: str | None = None) -> None:
        """
        Load existing log records from disk, creating the file if needed.

        Args:
            filepath: Path to the JSON log file. Defaults to LOG_FILENAME.
        """
        self.lock = threading.Lock()
        self.filepath: str = filepath or LOG_FILENAME

        ensure_dirs_exist(self.filepath)

        # Create the file if it doesn't exist, then load its contents.
        path = Path(self.filepath)
        path.touch(exist_ok=True)

        content = path.read_text().strip() or "[]"
        records: list[dict] = json.loads(content)
        self.records: List[LogRecord] = [LogRecord(r) for r in records]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(self, uri: str, status: int) -> None:
        """
        Append a new request record and flush to disk.

        Args:
            uri:    The request URI.
            status: The HTTP status code returned.
        """
        new_record = {"uri": uri, "status": status, "timestamp": time.time()}
        with self.lock:
            self.records.append(LogRecord(new_record))
        self._save()

    def get_logs_from_last_seconds(self, seconds: int = 60) -> List[LogRecord]:
        """
        Return records whose timestamps fall within the last *seconds* seconds,
        sorted chronologically (oldest first).

        Args:
            seconds: Look-back window in seconds. Defaults to 60.

        Returns:
            Filtered and sorted list of LogRecord objects.
        """
        now = datetime.now()
        recent: List[LogRecord] = [
            log for log in self.records
            if (now - datetime.fromtimestamp(log.timestamp)).total_seconds() <= seconds
        ]
        return sorted(recent, key=lambda log: log.timestamp)

    def as_list(self) -> list[dict]:
        """Return all records as a list of raw dicts (for serialization)."""
        return [r.as_dict() for r in self.records]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Serialize all records to disk as formatted JSON (thread-safe)."""
        with self.lock:
            content = json.dumps(self.as_list(), indent=2, separators=(",", ": "))
            with open(self.filepath, "w") as fp:
                fp.write(content)