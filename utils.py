from typing import Dict, Any
from collections import defaultdict
from functools import wraps
import json
import base64
import concurrent.futures
from pathlib import Path
import os
import time
import zlib

def convert_defaultdict_to_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively converts nested defaultdicts to dictionaries.
    """
    if isinstance(data, defaultdict):
        # Convert defaultdict to dictionary
        data = dict(data)
    
    if isinstance(data, dict):
        # Recursively convert nested dictionaries
        for key, value in data.items():
            data[key] = convert_defaultdict_to_dict(value)
    
    elif isinstance(data, list):
        # Recursively convert nested lists
        for i, item in enumerate(data):
            data[i] = convert_defaultdict_to_dict(item)
    
    return data

def ensure_dirs_exist(path: str):
    path: Path = Path(path)
    
    if len(path.parts) > 1:
        if '.' in path.parts[-1]:
            directories = '//'.join(path.parts[:-1])
        else:
            directories = '//'.join(path.parts)
            
        if not Path(directories).exists():
            os.makedirs(directories)

def singleton(cls):
    instances = {}

    # Preserve the original class for inspection
    @wraps(cls)
    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
            if hasattr(instances[cls], '__post_init__'):
                instances[cls].__post_init__()
        return instances[cls]

    return get_instance

class Profiler:
    def __init__(self):
        self.start_times = {}

    def start_profile(self, label: str) -> None:
        """Start a new profiling session for the given label."""
        self.start_times[label] = time.time()

    def end_profile(self, label: str) -> None:
        """End the profiling session for the given label and print the duration."""
        if label in self.start_times:
            elapsed_time = time.time() - self.start_times[label]
            if elapsed_time > 0.001:
                print(f"Time elapsed for '{label}': {elapsed_time:.4f} seconds")
        else:
            print(f"No profiling session found for label '{label}'.")

@singleton
class ClassRepository:
    def __init__(self):
        self.classes = {}

    def add_module_classes(self, globals):
        """Discover and store all globally defined classes."""
        global_vars = globals
        for name, obj in global_vars.items():
            if name != 'ClassRepository' and isinstance(obj, type):
                self.classes[name] = obj

    def add_class(self, cls):
        """Add a class to the dictionary."""
        
        if not isinstance(cls, type):
            raise TypeError("Input 'cls' must be a type")

        self.classes[cls.__name__] = cls

    def get_class(self, class_name):
        """Get a class from the dictionary."""
        return self.classes.get(class_name)

    def list_classes(self):
        """List all globally defined classes."""
        return list(self.classes.keys())

import pickle
from typing import Any, Dict

class PickleStore:
    @staticmethod
    def touch_file(default_obj: Any, filename: str) -> None:
        """Ensure file exists."""
        try:
            if not Path(filename).exists():
                PickleStore.save_object(default_obj, filename)
        except Exception as e:
            print(f"Failed to save object to {filename}: {e}")
            
    @staticmethod
    def save_object(obj: Any, filename: str) -> None:
        """Save an object to a file using pickle."""
        try:
            ensure_dirs_exist(filename)
            with open(filename, 'wb') as file:
                pickle.dump(obj, file)
        except Exception as e:
            print(f"Failed to save object to {filename}: {e}")

    @staticmethod
    def load_object(filename: str) -> Any:
        """Load an object from a file using pickle."""
        try:
            with open(filename, 'rb') as file:
                return pickle.load(file)
        except FileNotFoundError:
            print(f"No such file: {filename}")
        except Exception as e:
            print(f"Failed to load object from {filename}: {e}")
        return None

import zlib
import json

class DataSerializer:
    """
    A class for serializing and deserializing data dictionaries.
    Data is compressed, encoded, and then serialized to JSON during serialization,
    and reversed during deserialization.
    """

    @staticmethod
    def compress_text(text: str, level=6) -> str:
        """
        Compresses the given text, encodes it in base64, and returns the result.
        """
        # Compress text using zlib
        compressed_data = zlib.compress(text.encode('utf-8'), level)
        # Return the compressed data as string
        return compressed_data.hex()

    @staticmethod
    def decompress_text(hex_encoded: str) -> str:
        """
        Decodes the given base64-encoded text, decompresses it, and returns the result.
        """
        # Decode hex-encoded string
        compressed_data = bytes.fromhex(hex_encoded)
        # Decompress using zlib and return as string
        return zlib.decompress(compressed_data).decode('utf-8')

    @staticmethod
    def serialize_dict(data: dict, level=6) -> dict:
        """
        Serializes the given dictionary after compressing and encoding its values,
        and returns the resulting JSON string.
        """
        # Create a list to store the futures
        futures = []

        # Function to compress text asynchronously
        def compress(key, value):
            if isinstance(value, str):
                data[key] = ('str', DataSerializer.compress_text(value, level))
            elif isinstance(value, dict):
                value_str = json.dumps(value)
                data[key] = ('dict', DataSerializer.compress_text(value_str, level))
            else:
                raise ValueError(f'Serialization not supported for {type(value)}.')

        # Create a ProcessPoolExecutor with maximum workers
        with concurrent.futures.ProcessPoolExecutor() as executor:
            # Submit compression tasks for each key-value pair
            for key, value in data.items():
                futures.append(executor.submit(compress, key, value))
            
            # Wait for all tasks to complete
            concurrent.futures.wait(futures)

        # Serialize dictionary to JSON
        return json.dumps(data)

    @staticmethod
    def deserialize_dict(json_str) -> dict:
        """
        Deserializes the given JSON string, decodes and decompresses its values,
        and returns the resulting dictionary.
        """
        data: dict = json.loads(json_str)
        # Create a list to store the futures
        futures = []

        # Function to decompress text asynchronously
        def decompress(key, value):
            if value[0] == 'str':
                data[key] = DataSerializer.decompress_text(value[1])
            elif value[0] == 'dict':
                data[key] = json.loads(DataSerializer.decompress_text(value[1]))

        # Create a ProcessPoolExecutor with maximum workers
        with concurrent.futures.ProcessPoolExecutor() as executor:
            # Submit decompression tasks for each key-value pair
            for key, value in data.items():
                futures.append(executor.submit(decompress, key, value))
            
            # Wait for all tasks to complete
            concurrent.futures.wait(futures)

        return data
