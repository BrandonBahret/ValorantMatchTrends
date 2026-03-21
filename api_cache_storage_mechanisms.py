import json
import os
from typing import Dict, Type
from abc import ABC, abstractmethod

import threading
from pathlib import Path

from api_cache_record import CacheRecord
from chunked_dictionary import ChunkedDictionary
from utils import PickleStore, ensure_dirs_exist



class StorageMechanism(ABC):
    """Abstract base class for storage mechanisms."""
    
    def __init__(self, filepath: str):
        self.lock = threading.Lock()
        self.__filepath = filepath
        self.records: Dict[str, dict] = self.load()
    
    @property
    def filepath(self) -> Path:
        return Path(self.__filepath)
    
    def load(self):
        with self.lock:
            ensure_dirs_exist(self.filepath)
            self.touch_store()
            return self._impl__load(self.filepath)
        
    def save(self, data: dict):
        with self.lock:
            ensure_dirs_exist(self.filepath)
            self.touch_store()
            return self._impl__save(data, self.filepath)
    
    def get_record(self, key: str) -> CacheRecord:
        """Instantiate the CacheRecord for the provided key."""
        return CacheRecord(self.records[key])
    
    def update_record(self, key: str, data: dict):
        self._impl__update_record(key, data)
    
    def erase_everything(self):
        """Erases all records from datastore"""
        self._impl__erase_everything()
    
    def store_record(self, key: str, cache_record_dict: dict):
        """Store the CacheRecord with the provided key"""
        key = str(key) # Ensure key is string
        self.records[key] = cache_record_dict
        self.save(self.records)
    
    def touch_store(self):
        """Ensure datastore exists at filepath"""
        success = self._impl__touch_store(self.filepath)
        if not success:
            raise Exception("New datastore could not be created.")
    
    @abstractmethod
    def _impl__update_record(self, key: str, data: dict):
        """Update the data on the specified record"""
        pass
    
    @abstractmethod
    def _impl__erase_everything(self):
        """Erases all records from the datastore"""
        pass
        
    @abstractmethod
    def _impl__touch_store(self, filepath: Path) -> bool:
        """Ensure datastore exists at filepath"""
        pass

    @abstractmethod
    def _impl__load(self, filepath: Path) -> Dict[str, dict]:
        """Load cache records (as dicts) from storage."""
        pass

    @abstractmethod
    def _impl__save(self, cache_records_dict: Dict[str, dict], filepath: Path):
        """Save data to storage."""
        pass


class JSONStorage(StorageMechanism):
    """Concrete subclass of StorageMechanism for JSON storage."""
    
    def _impl__touch_store(self, filepath: Path) -> bool:
        Path(filepath).touch(exist_ok=True)
        return True

    def _impl__erase_everything(self):
        self.records = {}
        self.save(self.records)
    
    def _impl__update_record(self, key: str, data: Dict):
        record = self.get_record(key)
        record.update(data)
        self.save(self.records)
    
    def _impl__load(self, filepath: Path) -> Dict[str, Dict]:
        """Load data from a JSON file."""
        with open(filepath, 'r') as f:
            content = f.read().strip()
            
        records = json.loads(content) if content else {}
        return records
    
    def _impl__save(self, cache_records_dict: Dict[str, Dict], filepath: Path):
        """Save data to a JSON file."""
        data = cache_records_dict
        for key in data.keys():
            cast = data[key]['cast']
            if cast in ['set']:
                # Sets aren't seralizable, but we know to convert back by our cast info...
                data[key]['data'] = tuple(data[key]['data'])
        
        content = json.dumps(data)
        with open(filepath, 'w') as fp:
            fp.write(content)


class PickleStorage(StorageMechanism):
    """Concrete subclass of StorageMechanism for Pickle storage."""
    
    def _impl__touch_store(self, filepath: Path) -> bool:
        PickleStore.touch_file({}, filepath)
        return True

    def _impl__erase_everything(self):
        self.records = {}
        self.save(self.records)

    def _impl__update_record(self, key: str, data: Dict):
        record = self.get_record(key)
        record.update(data)
        self.save(self.records)
    
    def _impl__load(self, filepath: Path) -> Dict[str, Dict]:
        """Load data from a Pickle file."""
        records = PickleStore.load_object(filepath)
        return records
    
    def _impl__save(self, cache_records_dict: Dict[str, Dict], filepath: Path):
        """Save data to a Pickle file."""
        data = cache_records_dict
        PickleStore.save_object(data, filepath)


class ChunkedStorage(StorageMechanism):
    """Concrete subclass of StorageMechanism for ChunkedDictionary storage."""
    
    def _impl__touch_store(self, filepath: Path) -> bool:
        datastore_path = filepath.parent
        if not ChunkedDictionary.directory_contains_chunked_dictionary(datastore_path):
            self.chunked_dict = ChunkedDictionary.from_dict({}, datastore_path, 15*1024*1024)
        
        return ChunkedDictionary.directory_contains_chunked_dictionary(datastore_path)

    def _impl__erase_everything(self):
        self.chunked_dict.erase_everything()

    def _impl__update_record(self, key: str, data: Dict):
        record = self.get_record(key)
        record.update(data)
        self.chunked_dict[key] = record.as_dict()

    def _impl__load(self, filepath: Path) -> Dict[str, Dict]:
        """Load data from a ChunkedDictionary file."""
        self.chunked_dict = ChunkedDictionary.from_disk(filepath)
        return self.chunked_dict
    
    def _impl__save(self, cache_records_dict: Dict[str, Dict], filepath: Path):
        """Data is saved atomically as the records dictionary is manipulated. Updates manifest."""
        self.chunked_dict.manifest.save()

def get_storage_mechanism(filepath: str) -> Type[StorageMechanism]:
    """Return the appropriate storage mechanism subclass based on the file extension."""
    filepath: Path = Path(filepath)
    
    # Extract the file extension and convert to lowercase
    file_extension = filepath.suffix.lower()  
    strategies = {
        '.manifest': ChunkedStorage,
        '.json': JSONStorage,
        '.pkl': PickleStorage
    }
    
    mechanism = strategies.get(file_extension, None)
    
    if mechanism is None:
        # If no matching mechanism or '.manifest.json' files found, raise ValueError
        raise ValueError(f"No storage mechanism found for file extension: {file_extension}")
    
    return mechanism
