import json
import os
import math
from pathlib import Path
import threading
from typing import Any, Dict, Generator, List
import sys
from functools import cached_property as lazy_property

from api_cache_record import UNSET
from utils import ensure_dirs_exist
from utils import PickleStore


def get_size_of_dict(d: dict) -> int:
    """
    Recursively calculate the size of a dictionary and its nested dictionaries.
    """
    return len(json.dumps(d))

def chunk_dictionary(data: dict, chunk_size_in_bytes: int) -> Generator[dict, None, None]:
    '''Returns an iterator, chunking the input dictionary into chunks of size chunk_size_in_bytes'''
    items = list(data.items())
    total_size = 0
    chunk = {}
    
    for key, value in items:
        item_size = sys.getsizeof(key) + get_size_of_dict(value)
        
        # If adding the next item would exceed the chunk size, yield the current chunk
        if total_size + item_size > chunk_size_in_bytes:
            yield chunk
            chunk = {}
            total_size = 0
        
        chunk[key] = value
        total_size += item_size
    
    # Yield the remaining chunk
    if chunk or (data == {}):
        yield chunk
        
class ChunkedDictionaryManifest:
    def __init__(self, manifest_filepath: str):
        self.lock = threading.Lock()
        self.filepath = manifest_filepath
        
        with open(manifest_filepath, 'r') as fp:
            manifest = json.load(fp)
            
        self.chunks_map: dict = manifest['chunks_map']
        self.chunk_size_in_bytes: int = manifest['chunk_size_in_bytes']
        self.chunks_path = Path(manifest['chunks_path'])
        self.chunks_count: int = manifest['chunks_count']
    
    def is_chunk_filepath(self, file: str) -> bool:
        return file.startswith(str(self.chunks_path))
    
    @staticmethod
    def get_chunk_filename(index: int) -> str:
        return f'{index}-chunk.pkl'
    
    @staticmethod
    def get_chunk_index_from_filename(filename: str) -> int:
        return int(filename.replace('-chunk.pkl', ''))
    
    def remove_unused_chunks(self):
        with self.lock:
            # Remove unused chunk files
            for chunk_filename in os.listdir(self.chunks_path):
                index = ChunkedDictionaryManifest.get_chunk_index_from_filename(chunk_filename)
                if index+1 > self.chunks_count:
                    filepath = self.chunks_path / chunk_filename
                    os.remove(filepath)
                
    def erase_all_chunks_nonreversable(self):
        with self.lock:
            # Remove unused chunk files
            for chunk_filename in os.listdir(self.chunks_path):
                if chunk_filename.endswith('.pkl'):
                    filepath = self.chunks_path / chunk_filename
                    os.remove(filepath)
            self.chunks_map = {}
            self.chunks_count = 0

    def save(self):
        with self.lock:
            manifest = {
                'chunk_size_in_bytes': self.chunk_size_in_bytes,
                'chunks_path': str(self.chunks_path),
                'chunks_count': self.chunks_count,
                'chunks_map': self.chunks_map
            }
            with open(self.filepath, 'w') as fp:
                json.dump(manifest, fp, indent=2)


class ChunkedDictionary:
    def __init__(self, manifest_filepath: str):
        self.lock = threading.Lock()
        self.manifest = ChunkedDictionaryManifest(manifest_filepath)
        self.manifest.remove_unused_chunks()
        self.chunks: Dict[str, dict] = {}
        
    def data(self) -> dict:
        '''loads entire datastore into dictionary'''
        return {k:self.__getitem__(k) for k in self.keys()}
    
    def erase_everything(self):
        '''Erases every record from the ChunkedDictionary'''
        with self.lock:
            self.chunks: Dict[str, dict] = {}
        
        self.manifest.erase_all_chunks_nonreversable()
        self.manifest.save()
    
    def __contains__(self, key: str) -> bool:
        return key in self.keys()

    def __len__(self) -> int:
        return len(self.keys())
    
    def items(self):
        return self.data().items()
    
    def keys(self) -> List[str]:
        return self.manifest.chunks_map.keys()
    
    def get(self, key: str, default_value=UNSET):
        if default_value != UNSET and (key not in self.keys()):
            return default_value
        return self.__getitem__(key)
    
    def __getitem__(self, key: str):
        chunk_filename = self.manifest.chunks_map[key]
        chunk = self.get_chunk(chunk_filename)
        return chunk[key]
    
    def __setitem__(self, key: str, value: Any):
        
        chunk_filename = None

        if key in self.manifest.chunks_map:
            # Update value in chunks
            chunk_filename = self.manifest.chunks_map[key]
            chunk = self.get_chunk(chunk_filename)
            
            with self.lock:
                chunk[key] = value
        else:
            # Either create new chunk, or insert in existing one.
            last_chunk_index = self.manifest.chunks_count - 1
            if last_chunk_index == -1:
                last_chunk_index = 0
                last_chunk_filename = self.create_new_chunk()
            else:
                last_chunk_filename = ChunkedDictionaryManifest.get_chunk_filename(last_chunk_index)
            
            # Calculate the size of the last chunk in datastore
            last_chunk = self.get_chunk(last_chunk_filename)
            last_chunk_size = get_size_of_dict(last_chunk)
            
            if last_chunk_size + get_size_of_dict({key: value}) < self.manifest.chunk_size_in_bytes:
                chunk_filename = last_chunk_filename
            else:
                chunk_filename = self.create_new_chunk()
            
            with self.lock:
                self.manifest.chunks_map[key] = chunk_filename
                self.chunks[chunk_filename][key] = value
        
        assert chunk_filename is not None
        self.save_chunk(chunk_filename)

    def create_new_chunk(self) -> str:
        '''Creates a new chunk file and returns its key for self.chunks'''
        
        with self.lock:
            index = self.manifest.chunks_count
            chunk_filename = ChunkedDictionaryManifest.get_chunk_filename(index)
            chunk_filepath = self.manifest.chunks_path / chunk_filename
                    
            PickleStore.save_object({}, chunk_filepath)
        
            self.chunks[chunk_filename] = {}
            self.manifest.chunks_count += 1
        
        return chunk_filename
    
    def get_chunk(self, chunk_filename: str) -> dict:
        chunk_filename = str(chunk_filename)
        assert not self.manifest.is_chunk_filepath(chunk_filename), 'Filename, not a path.'
        
        if not chunk_filename in self.chunks:
            chunk_filepath = str(self.manifest.chunks_path / chunk_filename)
            
            data = PickleStore.load_object(chunk_filepath)
            self.chunks[chunk_filename] = data
        
        return self.chunks[chunk_filename]
    
    def resize_data_chunks(self, chunk_size_in_bytes: int):
        data = {k:self.get(k) for k in self.keys()}
        manifest_filepath: Path = self.manifest.filepath
        destination_path = manifest_filepath.parent
        self.erase_everything()
        new_datastore = ChunkedDictionary.from_dict(data, destination_path, chunk_size_in_bytes)
        self.chunks = new_datastore.chunks
        self.manifest = new_datastore.manifest
        self.lock = new_datastore.lock
        self.manifest.save()
    
    def save_chunk(self, chunk_filename: str):
        '''Save data under the key 'chunk_filename.' Manifest will also be saved with its current state.'''
        with self.lock:
            chunk = self.chunks[chunk_filename]
            chunk_filepath = self.manifest.chunks_path / chunk_filename
            
            PickleStore.save_object(chunk, chunk_filepath)
        
        self.manifest.save()
    
    @staticmethod
    def directory_contains_chunked_dictionary(datastore_path: str):
        datastore_path: Path = Path(datastore_path)
        
        if datastore_path.is_dir():
            # Check if any file in the directory has the extension '.manifest'
            is_manifest_file = lambda file: file.endswith('.manifest')
            if any([is_manifest_file(f) for f in os.listdir(datastore_path)]):
                return True
        
        return False
    
    @staticmethod
    def from_disk(manifest_filepath: str) -> 'ChunkedDictionary':
        '''datastore_path must be the root directory of the datastore'''
        manifest_filepath: Path = Path(manifest_filepath)
        extension = manifest_filepath.suffix
        if extension != '.manifest':
            raise Exception(f"{str(manifest_filepath)!r} should have the '.manifest' extension for a ChunkedDictionary manifest file!")
  
        return ChunkedDictionary(manifest_filepath)

    @staticmethod
    def from_dict(data: dict, destination_path:str, chunk_size_in_bytes:int) -> 'ChunkedDictionary':
        '''destination_path will become the root directory of the new datastore'''
        
        destination = Path(destination_path)
        ensure_dirs_exist(destination)

        chunks_path = destination / 'chunks'
        ensure_dirs_exist(chunks_path)
        
        # Write chunks
        chunks_map = {}
        for index, chunk in enumerate(chunk_dictionary(data, chunk_size_in_bytes)):
            chunk_filename = ChunkedDictionaryManifest.get_chunk_filename(index)
            chunk_filepath = chunks_path / chunk_filename
            
            chunks_map.update({k: str(chunk_filename) for k in chunk.keys()})
            PickleStore.save_object(chunk, chunk_filepath)
        
        # Write manifest        
        manifest_path = destination / f'chunks.manifest'
        with open(manifest_path, 'w') as fp:
            manifest = {
                'chunk_size_in_bytes': chunk_size_in_bytes,
                'chunks_path': str(chunks_path),
                'chunks_count': index+1,
                'chunks_map': chunks_map
            }
            json.dump(manifest, fp, indent=2)
            
        return ChunkedDictionary(manifest_path)
