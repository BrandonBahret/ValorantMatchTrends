import math
import time
from typing import Any, Optional

from api_cache_record import UNSET, CacheRecord
from api_cache_storage_mechanisms import get_storage_mechanism
from utils import ClassRepository


CACHE_FILE = 'api-cache.pkl'


class Cache:
    """Manages persistent caching of API responses with optional TTL and type casting."""

    def __init__(self, filepath: Optional[str] = None):
        """
        Initialize the cache with a storage backend determined by file extension.

        Args:
            filepath: Path to the cache file. Defaults to CACHE_FILE ('api-cache.pkl').
        """
        self.classes = ClassRepository()

        filepath = filepath or CACHE_FILE
        StorageClass = get_storage_mechanism(filepath)
        self.storage = StorageClass(filepath)

    @staticmethod
    def cached(cls):
        """
        Class decorator that registers a class as cache-compatible.

        Usage:
            @Cache.cached
            class MyAPIResponse:
                ...
        """
        ClassRepository().add_class(cls)
        return cls

    def has(self, key: str) -> bool:
        """Return True if a cache record exists for the given key."""
        return key in self.storage.records

    def is_data_fresh(self, key: str) -> bool:
        """
        Return True if a non-stale cache record exists for the given key.
        Returns False if the key is missing or the record has expired.
        """
        if not self.has(key):
            return False
        return not self.storage.get_record(key).is_data_stale

    def get(self, key: str) -> CacheRecord:
        """
        Retrieve the raw CacheRecord for the given key.

        Raises:
            KeyError: If no record exists for the key.
        """
        if not self.has(key):
            raise KeyError(f'No cache found for {key!r}!')
        return self.storage.get_record(key)

    def get_object(self, key: str, default_value: Any = UNSET) -> object:
        """
        Retrieve the cached value for a key, cast to its registered type.

        Args:
            key:           Cache key to look up.
            default_value: Returned if the key is missing. Raises KeyError if omitted.

        Raises:
            KeyError:      If the key is missing and no default was provided.
            AttributeError: If the record has no cast type registered.
        """
        if not self.has(key):
            if default_value is UNSET:
                raise KeyError(f'No cache found for {key!r}!')
            return default_value

        record = self.storage.get_record(key)
        if not record.should_convert_type:
            raise AttributeError(f'No cast type provided for {key!r}!')
        return record.cast(record.data)

    def update(self, key: str, data: dict):
        """
        Update the data payload of an existing cache record.

        Raises:
            KeyError: If no record exists for the key.
        """
        if not self.has(key):
            raise KeyError(f'No cache found for {key!r}!')
        self.storage.update_record(key, data)

    def store(self, key: str, data: dict, expiry: int = math.inf, cast: type = None):
        """
        Create or overwrite a cache record.

        Args:
            key:    Unique identifier for this cache entry.
            data:   The payload to cache.
            expiry: Seconds until the record is considered stale. Defaults to no expiry.
            cast:   Optional type to register for deserialising the cached data.
        """
        # Serialise math.inf as a string so it survives pickle/JSON round-trips
        serialisable_expiry = 'math.inf' if expiry == math.inf else expiry

        new_record = {
            'cast': cast.__name__ if isinstance(cast, type) else None,
            'expiry': serialisable_expiry,
            'timestamp': time.time(),
            'data': data,
        }
        self.storage.store_record(key, new_record)

    def completely_erase_cache(self):
        """Permanently delete all records from the cache storage."""
        self.storage.erase_everything()