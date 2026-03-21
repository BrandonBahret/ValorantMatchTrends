"""
chunked_dictionary.py

Provides a disk-backed dictionary that splits its contents across multiple
pickle "chunk" files. A JSON manifest file tracks which chunk holds each key,
allowing large datasets to be stored and accessed without loading everything
into memory at once.

Typical usage:
    # Build from an in-memory dict
    store = ChunkedDictionary.from_dict(data, "/path/to/dir", chunk_size_in_bytes=1_000_000)

    # Re-open an existing store
    store = ChunkedDictionary.from_disk("/path/to/dir/chunks.manifest")

    # Use like a regular dict
    store["my_key"] = {"some": "value"}
    value = store["my_key"]
"""

import json
import math
import os
import sys
import threading
from functools import cached_property as lazy_property
from pathlib import Path
from typing import Any, Dict, Generator, Iterator, List, Optional

from api_cache_record import UNSET
from utils import ensure_dirs_exist, PickleStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_size_of_dict(d: dict) -> int:
    """Return the byte-length of *d* when serialised to a compact JSON string.

    This is an approximation: it counts UTF-8 characters in the JSON
    representation rather than true in-memory size, but it is consistent and
    cheap to compute.

    Args:
        d: Any JSON-serialisable dictionary.

    Returns:
        Number of characters in ``json.dumps(d)``.
    """
    return len(json.dumps(d))


def chunk_dictionary(
    data: dict,
    chunk_size_in_bytes: int,
) -> Generator[dict, None, None]:
    """Split *data* into sub-dictionaries whose estimated size stays under the limit.

    Items are grouped in insertion order. When adding the next item would push
    the running total above *chunk_size_in_bytes*, the current accumulator is
    yielded and a fresh one is started.

    Args:
        data: The dictionary to split.
        chunk_size_in_bytes: Maximum estimated byte-size of each yielded chunk.

    Yields:
        Sub-dictionaries, each within the size budget.  An empty input dict
        yields a single empty dict so that the manifest can still be written.
    """
    chunk: dict = {}
    total_size: int = 0

    for key, value in data.items():
        item_size = sys.getsizeof(key) + get_size_of_dict(value)

        if total_size + item_size > chunk_size_in_bytes:
            yield chunk
            chunk = {}
            total_size = 0

        chunk[key] = value
        total_size += item_size

    # Always yield at least one chunk (even if the input was empty).
    if chunk or not data:
        yield chunk


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class ChunkedDictionaryManifest:
    """Reads and writes the JSON manifest that describes a :class:`ChunkedDictionary`.

    The manifest stores:
    - ``chunk_size_in_bytes`` – target byte-budget per chunk file.
    - ``chunks_path``         – absolute path to the directory containing ``.pkl`` files.
    - ``chunks_count``        – total number of chunk files that should exist.
    - ``chunks_map``          – mapping of ``{key: chunk_filename}``.

    All mutating operations (``save``, ``erase_all_chunks_nonreversable``,
    ``remove_unused_chunks``) are protected by a re-entrant threading lock so
    the manifest can be shared across threads safely.

    Args:
        manifest_filepath: Path to an existing ``.manifest`` JSON file.
    """

    def __init__(self, manifest_filepath: str) -> None:
        self.lock = threading.Lock()
        self.filepath: Path = Path(manifest_filepath)

        with open(manifest_filepath, "r") as fp:
            manifest: dict = json.load(fp)

        self.chunks_map: Dict[str, str] = manifest["chunks_map"]
        self.chunk_size_in_bytes: int = manifest["chunk_size_in_bytes"]
        self.chunks_path: Path = Path(manifest["chunks_path"])
        self.chunks_count: int = manifest["chunks_count"]

    # ------------------------------------------------------------------
    # Filename helpers
    # ------------------------------------------------------------------

    def is_chunk_filepath(self, file: str) -> bool:
        """Return ``True`` if *file* is an absolute path inside ``chunks_path``."""
        return file.startswith(str(self.chunks_path))

    @staticmethod
    def get_chunk_filename(index: int) -> str:
        """Return the canonical filename for chunk number *index* (e.g. ``"0-chunk.pkl"``)."""
        return f"{index}-chunk.pkl"

    @staticmethod
    def get_chunk_index_from_filename(filename: str) -> int:
        """Parse and return the numeric index from a chunk filename."""
        return int(filename.replace("-chunk.pkl", ""))

    # ------------------------------------------------------------------
    # Chunk file management
    # ------------------------------------------------------------------

    def remove_unused_chunks(self) -> None:
        """Delete any ``.pkl`` files on disk whose index is >= ``chunks_count``.

        This cleans up leftover files that can appear after a resize or erase.
        """
        with self.lock:
            for chunk_filename in os.listdir(self.chunks_path):
                index = ChunkedDictionaryManifest.get_chunk_index_from_filename(chunk_filename)
                if index + 1 > self.chunks_count:
                    filepath = self.chunks_path / chunk_filename
                    os.remove(filepath)

    def erase_all_chunks_nonreversable(self) -> None:
        """Delete every ``.pkl`` file under ``chunks_path`` and reset in-memory state.

        .. warning::
            This operation is **irreversible**.  All stored data will be lost.
        """
        with self.lock:
            for chunk_filename in os.listdir(self.chunks_path):
                if chunk_filename.endswith(".pkl"):
                    filepath = self.chunks_path / chunk_filename
                    os.remove(filepath)

            self.chunks_map = {}
            self.chunks_count = 0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Serialise the manifest's current state to disk as a JSON file."""
        with self.lock:
            manifest = {
                "chunk_size_in_bytes": self.chunk_size_in_bytes,
                "chunks_path": str(self.chunks_path),
                "chunks_count": self.chunks_count,
                "chunks_map": self.chunks_map,
            }
            with open(self.filepath, "w") as fp:
                json.dump(manifest, fp, indent=2)


# ---------------------------------------------------------------------------
# ChunkedDictionary
# ---------------------------------------------------------------------------

class ChunkedDictionary:
    """A disk-backed dictionary whose entries are spread across pickle chunk files.

    Chunks are loaded lazily and cached in ``self.chunks`` for the lifetime of
    this object.  Every write is immediately flushed to disk so the store
    remains consistent even if the process is interrupted.

    Args:
        manifest_filepath: Path to an existing ``.manifest`` JSON file.
    """

    def __init__(self, manifest_filepath: str) -> None:
        self.lock = threading.Lock()
        self.manifest = ChunkedDictionaryManifest(manifest_filepath)
        self.manifest.remove_unused_chunks()

        # In-memory cache: chunk_filename -> {key: value, ...}
        self.chunks: Dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Bulk access
    # ------------------------------------------------------------------

    def data(self) -> dict:
        """Return all entries as a plain dictionary (loads every chunk into memory)."""
        return {k: self[k] for k in self.keys()}

    def erase_everything(self) -> None:
        """Remove every record from this store and reset it to an empty state.

        .. warning::
            This operation is **irreversible**.
        """
        with self.lock:
            self.chunks = {}

        self.manifest.erase_all_chunks_nonreversable()
        self.manifest.save()

    # ------------------------------------------------------------------
    # dict-like interface
    # ------------------------------------------------------------------

    def __contains__(self, key: str) -> bool:
        return key in self.keys()

    def __len__(self) -> int:
        return len(self.keys())

    def items(self) -> Iterator:
        """Return a view of all (key, value) pairs (loads every chunk)."""
        return self.data().items()

    def keys(self) -> List[str]:
        """Return all stored keys (read from the manifest; no chunks loaded)."""
        return list(self.manifest.chunks_map.keys())

    def get(self, key: str, default_value: Any = UNSET) -> Any:
        """Return the value for *key*, or *default_value* if the key is absent.

        Args:
            key: The key to look up.
            default_value: Returned when *key* is not present.  If omitted and
                the key is missing, a :class:`KeyError` is raised (via
                ``__getitem__``).
        """
        if default_value is not UNSET and key not in self.keys():
            return default_value
        return self[key]

    def __getitem__(self, key: str) -> Any:
        """Return the value associated with *key*.

        Raises:
            KeyError: If *key* is not in the store.
        """
        chunk_filename: str = self.manifest.chunks_map[key]
        chunk = self.get_chunk(chunk_filename)
        return chunk[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Store *value* under *key*, creating or extending a chunk as needed.

        If *key* already exists its chunk is updated in-place.  Otherwise the
        value is appended to the most recent chunk if it fits within the size
        budget, or a new chunk is created if not.
        """
        chunk_filename: Optional[str] = None

        if key in self.manifest.chunks_map:
            # --- Update existing key in its current chunk ---
            chunk_filename = self.manifest.chunks_map[key]
            chunk = self.get_chunk(chunk_filename)
            with self.lock:
                chunk[key] = value

        else:
            # --- Insert new key: find or create an appropriate chunk ---
            last_chunk_index = self.manifest.chunks_count - 1

            if last_chunk_index == -1:
                # Store is completely empty — create the very first chunk.
                last_chunk_index = 0
                last_chunk_filename = self.create_new_chunk()
            else:
                last_chunk_filename = ChunkedDictionaryManifest.get_chunk_filename(last_chunk_index)

            last_chunk = self.get_chunk(last_chunk_filename)
            last_chunk_size = get_size_of_dict(last_chunk)

            if last_chunk_size + get_size_of_dict({key: value}) < self.manifest.chunk_size_in_bytes:
                # The new item fits in the last chunk.
                chunk_filename = last_chunk_filename
            else:
                # The last chunk is full — start a new one.
                chunk_filename = self.create_new_chunk()

            with self.lock:
                self.manifest.chunks_map[key] = chunk_filename
                self.chunks[chunk_filename][key] = value

        assert chunk_filename is not None, "chunk_filename must be set before saving."
        self.save_chunk(chunk_filename)

    # ------------------------------------------------------------------
    # Chunk management
    # ------------------------------------------------------------------

    def create_new_chunk(self) -> str:
        """Allocate a new, empty chunk file on disk and register it in the manifest.

        Returns:
            The filename (not the full path) of the newly created chunk.
        """
        with self.lock:
            index = self.manifest.chunks_count
            chunk_filename = ChunkedDictionaryManifest.get_chunk_filename(index)
            chunk_filepath = self.manifest.chunks_path / chunk_filename

            PickleStore.save_object({}, chunk_filepath)
            self.chunks[chunk_filename] = {}
            self.manifest.chunks_count += 1

        return chunk_filename

    def get_chunk(self, chunk_filename: str) -> dict:
        """Return the in-memory dict for *chunk_filename*, loading from disk if needed.

        Args:
            chunk_filename: The bare filename (e.g. ``"0-chunk.pkl"``), **not**
                the full path.  Passing a full path raises :class:`AssertionError`.

        Returns:
            The deserialized chunk dictionary.
        """
        chunk_filename = str(chunk_filename)
        assert not self.manifest.is_chunk_filepath(chunk_filename), (
            "Expected a filename, not a full path."
        )

        if chunk_filename not in self.chunks:
            chunk_filepath = str(self.manifest.chunks_path / chunk_filename)
            self.chunks[chunk_filename] = PickleStore.load_object(chunk_filepath)

        return self.chunks[chunk_filename]

    def save_chunk(self, chunk_filename: str) -> None:
        """Flush the in-memory chunk and the manifest to disk.

        Args:
            chunk_filename: Bare filename of the chunk to persist.
        """
        with self.lock:
            chunk = self.chunks[chunk_filename]
            chunk_filepath = self.manifest.chunks_path / chunk_filename
            PickleStore.save_object(chunk, chunk_filepath)

        self.manifest.save()

    def resize_data_chunks(self, chunk_size_in_bytes: int) -> None:
        """Re-partition all data into chunks of a new target size.

        Reads every entry into memory, erases the store, then rebuilds it
        from scratch with the new chunk budget.  The manifest filepath and
        directory are preserved.

        Args:
            chunk_size_in_bytes: New target byte-size per chunk.
        """
        # Snapshot all data before erasing.
        data = {k: self.get(k) for k in self.keys()}
        manifest_filepath: Path = self.manifest.filepath
        destination_path = manifest_filepath.parent

        self.erase_everything()

        new_store = ChunkedDictionary.from_dict(data, destination_path, chunk_size_in_bytes)

        # Swap internal state to match the rebuilt store.
        self.chunks = new_store.chunks
        self.manifest = new_store.manifest
        self.lock = new_store.lock
        self.manifest.save()

    # ------------------------------------------------------------------
    # Class / static constructors
    # ------------------------------------------------------------------

    @staticmethod
    def directory_contains_chunked_dictionary(datastore_path: str) -> bool:
        """Return ``True`` if *datastore_path* contains at least one ``.manifest`` file."""
        path = Path(datastore_path)
        if path.is_dir():
            return any(f.endswith(".manifest") for f in os.listdir(path))
        return False

    @staticmethod
    def from_disk(manifest_filepath: str) -> "ChunkedDictionary":
        """Load an existing :class:`ChunkedDictionary` from a manifest file.

        Args:
            manifest_filepath: Path to a ``.manifest`` file created by
                :meth:`from_dict`.

        Returns:
            A fully initialised :class:`ChunkedDictionary` instance.

        Raises:
            Exception: If the path does not end with ``.manifest``.
        """
        path = Path(manifest_filepath)
        if path.suffix != ".manifest":
            raise Exception(
                f"{str(path)!r} must have the '.manifest' extension."
            )
        return ChunkedDictionary(path)

    @staticmethod
    def from_dict(
        data: dict,
        destination_path: str,
        chunk_size_in_bytes: int,
    ) -> "ChunkedDictionary":
        """Create a new :class:`ChunkedDictionary` on disk from an in-memory dict.

        Chunk files and a manifest are written to *destination_path*.  The
        directory (and a ``chunks/`` sub-directory) will be created if they
        do not already exist.

        Args:
            data: Source dictionary whose contents will be chunked and stored.
            destination_path: Root directory for the new datastore.
            chunk_size_in_bytes: Target byte-size for each chunk file.

        Returns:
            A :class:`ChunkedDictionary` backed by the newly written files.
        """
        destination = Path(destination_path)
        ensure_dirs_exist(destination)

        chunks_path = destination / "chunks"
        ensure_dirs_exist(chunks_path)

        # Write each chunk to disk and build the key → filename map.
        chunks_map: Dict[str, str] = {}
        index: int = 0
        for index, chunk in enumerate(chunk_dictionary(data, chunk_size_in_bytes)):
            chunk_filename = ChunkedDictionaryManifest.get_chunk_filename(index)
            chunk_filepath = chunks_path / chunk_filename

            chunks_map.update({k: str(chunk_filename) for k in chunk.keys()})
            PickleStore.save_object(chunk, chunk_filepath)

        # Write the manifest that ties everything together.
        manifest_path = destination / "chunks.manifest"
        with open(manifest_path, "w") as fp:
            manifest = {
                "chunk_size_in_bytes": chunk_size_in_bytes,
                "chunks_path": str(chunks_path),
                "chunks_count": index + 1,
                "chunks_map": chunks_map,
            }
            json.dump(manifest, fp, indent=2)

        return ChunkedDictionary(manifest_path)