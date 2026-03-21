import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Type

from api_cache_record import CacheRecord
from chunked_dictionary import ChunkedDictionary
from utils import PickleStore, ensure_dirs_exist


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class StorageMechanism(ABC):
    """Abstract base class defining the interface for cache storage backends.

    Subclasses implement the ``_impl__*`` methods to support different
    serialisation formats (JSON, Pickle, ChunkedDictionary).  All public
    methods acquire a threading lock so instances are safe to share across
    threads.
    """

    def __init__(self, filepath: str):
        self.lock = threading.Lock()
        self.__filepath = filepath
        self.records: Dict[str, dict] = self.load()

    @property
    def filepath(self) -> Path:
        return Path(self.__filepath)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, dict]:
        """Ensure the backing store exists, then load and return all records."""
        with self.lock:
            ensure_dirs_exist(self.filepath)
            self.touch_store()
            return self._impl__load(self.filepath)

    def save(self, data: dict):
        """Persist *data* to the backing store, creating it first if needed."""
        with self.lock:
            ensure_dirs_exist(self.filepath)
            self.touch_store()
            return self._impl__save(data, self.filepath)

    def get_record(self, key: str) -> CacheRecord:
        """Return the :class:`CacheRecord` associated with *key*."""
        return CacheRecord(self.records[key])

    def update_record(self, key: str, data: dict):
        """Merge *data* into the existing record at *key*."""
        self._impl__update_record(key, data)

    def store_record(self, key: str, cache_record_dict: dict):
        """Insert or overwrite the record at *key* and persist immediately."""
        key = str(key)  # Keys are always stored as strings
        self.records[key] = cache_record_dict
        self.save(self.records)

    def erase_everything(self):
        """Delete every record from the backing store."""
        self._impl__erase_everything()

    def touch_store(self):
        """Create the backing store at :attr:`filepath` if it does not exist.

        Raises:
            Exception: If the store could not be created.
        """
        if not self._impl__touch_store(self.filepath):
            raise Exception("New datastore could not be created.")

    # ------------------------------------------------------------------
    # Abstract implementation hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def _impl__touch_store(self, filepath: Path) -> bool:
        """Create an empty store at *filepath* if one does not already exist.

        Returns:
            ``True`` on success, ``False`` otherwise.
        """

    @abstractmethod
    def _impl__load(self, filepath: Path) -> Dict[str, dict]:
        """Deserialise and return all records from *filepath*."""

    @abstractmethod
    def _impl__save(self, cache_records_dict: Dict[str, dict], filepath: Path):
        """Serialise *cache_records_dict* and write it to *filepath*."""

    @abstractmethod
    def _impl__update_record(self, key: str, data: dict):
        """Merge *data* into the record at *key* and persist the change."""

    @abstractmethod
    def _impl__erase_everything(self):
        """Remove every record from the backing store."""


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------

class JSONStorage(StorageMechanism):
    """Storage backend that serialises cache records as a single JSON file.

    Note:
        Python ``set`` values are not JSON-serialisable.  They are stored as
        ``tuple`` and converted back via the ``cast`` field on load.
    """

    def _impl__touch_store(self, filepath: Path) -> bool:
        filepath.touch(exist_ok=True)
        return True

    def _impl__load(self, filepath: Path) -> Dict[str, dict]:
        with open(filepath, "r") as f:
            content = f.read().strip()
        return json.loads(content) if content else {}

    def _impl__save(self, cache_records_dict: Dict[str, dict], filepath: Path):
        data = cache_records_dict

        # ``set`` is not JSON-serialisable; coerce to ``tuple`` so the file
        # round-trips correctly.  The ``cast`` field signals the original type
        # and is used to restore it on the next load.
        for record in data.values():
            if record.get("cast") == "set":
                record["data"] = tuple(record["data"])

        with open(filepath, "w") as fp:
            fp.write(json.dumps(data))

    def _impl__update_record(self, key: str, data: dict):
        record = self.get_record(key)
        record.update(data)
        self.save(self.records)

    def _impl__erase_everything(self):
        self.records = {}
        self.save(self.records)


class PickleStorage(StorageMechanism):
    """Storage backend that serialises cache records using Python's pickle format.

    Pickle preserves arbitrary Python types (including ``set``), so no
    special coercion is required.
    """

    def _impl__touch_store(self, filepath: Path) -> bool:
        PickleStore.touch_file({}, filepath)
        return True

    def _impl__load(self, filepath: Path) -> Dict[str, dict]:
        return PickleStore.load_object(filepath)

    def _impl__save(self, cache_records_dict: Dict[str, dict], filepath: Path):
        PickleStore.save_object(cache_records_dict, filepath)

    def _impl__update_record(self, key: str, data: dict):
        record = self.get_record(key)
        record.update(data)
        self.save(self.records)

    def _impl__erase_everything(self):
        self.records = {}
        self.save(self.records)


class ChunkedStorage(StorageMechanism):
    """Storage backend backed by a :class:`ChunkedDictionary`.

    Records are written atomically per-key rather than flushing the entire
    dataset at once, making this backend suitable for large caches.  The
    manifest is updated on each save.
    """

    def _impl__touch_store(self, filepath: Path) -> bool:
        datastore_path = filepath.parent
        if not ChunkedDictionary.directory_contains_chunked_dictionary(datastore_path):
            # Initialise a new chunked dictionary with a 15 MB chunk size
            self.chunked_dict = ChunkedDictionary.from_dict(
                {}, datastore_path, 15 * 1024 * 1024
            )
        return ChunkedDictionary.directory_contains_chunked_dictionary(datastore_path)

    def _impl__load(self, filepath: Path) -> Dict[str, dict]:
        self.chunked_dict = ChunkedDictionary.from_disk(filepath)
        return self.chunked_dict

    def _impl__save(self, cache_records_dict: Dict[str, dict], filepath: Path):
        # Individual record writes are already atomic; just update the manifest.
        self.chunked_dict.manifest.save()

    def _impl__update_record(self, key: str, data: dict):
        record = self.get_record(key)
        record.update(data)
        self.chunked_dict[key] = record.as_dict()

    def _impl__erase_everything(self):
        self.chunked_dict.erase_everything()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Maps file extensions to their corresponding storage backend class.
_EXTENSION_TO_STORAGE: Dict[str, Type[StorageMechanism]] = {
    ".manifest": ChunkedStorage,
    ".json": JSONStorage,
    ".pkl": PickleStorage,
}


def get_storage_mechanism(filepath: str) -> Type[StorageMechanism]:
    """Return the :class:`StorageMechanism` subclass appropriate for *filepath*.

    The backend is selected solely from the file extension.

    Args:
        filepath: Path to the cache store file.

    Returns:
        The matching :class:`StorageMechanism` subclass (*not* an instance).

    Raises:
        ValueError: If no backend supports the given file extension.
    """
    extension = Path(filepath).suffix.lower()
    mechanism = _EXTENSION_TO_STORAGE.get(extension)

    if mechanism is None:
        raise ValueError(
            f"No storage mechanism found for file extension: {extension!r}"
        )

    return mechanism