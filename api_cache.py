from typing import Any, Optional

import time
import math

from api_cache_storage_mechanisms import get_storage_mechanism
from api_cache_record import UNSET, CacheRecord
from utils import ClassRepository


CACHE_FILE = 'api-cache.pkl'

class Cache:
    def __init__(self, filepath: Optional[str] = None):
        # Initialize class repository
        self.classes = ClassRepository()

        # Determine filepath
        filepath = filepath or CACHE_FILE
        StorageClass = get_storage_mechanism(filepath)
        self.storage = StorageClass(filepath)

    @staticmethod
    def cached(cls):
        '''Used to mark classes as cachable'''
        ClassRepository().add_class(cls)
        return cls
    
    def has(self, key: str):
        return key in self.storage.records.keys()
    
    def is_data_fresh(self, key: str):
        if not self.has(key):
            return False
        
        record = self.storage.get_record(key)
        return not record.is_data_stale
    
    def get(self, key: str) -> CacheRecord:
        if not self.has(key):
            raise KeyError(f'No cache found for {key!r}!')
        else:
            return self.storage.get_record(key)
    
    def get_object(self, key: str, default_value: Any = UNSET) -> object:
        if not self.has(key):
            if default_value == UNSET:
                raise KeyError(f'No cache found for {key!r}!')
            else:
                return default_value
        
        record = self.storage.get_record(key)
        if not record.should_convert_type:
            raise AttributeError(f'No cast type provided for {key!r}!')
        else:
            return record.cast(record.data)
    
    def update(self, key: str, data: dict):
        if not self.has(key):
            raise KeyError(f'No cache found for {key!r}!')
        
        self.storage.update_record(key, data)
    
    def store(self, key: str, data: dict, expiry: int=math.inf, cast: type=None):
        substitute_inf = lambda v: 'math.inf' if v == math.inf else v
        expiry = substitute_inf(expiry)
        new_record = {'cast': None, 'expiry': expiry, 'timestamp': time.time(), 'data': data}
        if isinstance(cast, type):
            new_record['cast'] = cast.__name__
        
        self.storage.store_record(key, new_record)
        
    def completely_erase_cache(self):
        self.storage.erase_everything()
