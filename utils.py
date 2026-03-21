import json
import os
import sys
import time
import zlib
import pickle
import concurrent.futures
from collections import defaultdict
from functools import wraps
from pathlib import Path
import subprocess
from typing import Any, Callable, Dict, List, Type, TypeVar


T = TypeVar("T")



# ---------------------------------------------------------------------------
# Dictionary helpers
# ---------------------------------------------------------------------------

def convert_defaultdict_to_dict(data: Any) -> Any:
    """Recursively convert nested ``defaultdict`` instances to plain ``dict``.

    Also traverses nested lists so that defaultdicts at any depth are converted.

    Args:
        data: The object to convert. Non-dict/list values are returned as-is.

    Returns:
        The same structure with every ``defaultdict`` replaced by a ``dict``.
    """
    if isinstance(data, defaultdict):
        data = dict(data)

    if isinstance(data, dict):
        for key, value in data.items():
            data[key] = convert_defaultdict_to_dict(value)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            data[i] = convert_defaultdict_to_dict(item)

    return data


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def open_folder(path: Path) -> None:
    """Open *path* in the system file explorer, cross-platform."""
    path = path.resolve()
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)

def ensure_dirs_exist(path: str) -> None:
    """Create all intermediate directories required for *path* if they don't exist.

    If the final component of *path* contains a ``.`` it is treated as a
    filename and only its parent directories are created; otherwise the full
    path is treated as a directory tree and created in its entirety.

    Args:
        path: A file or directory path whose parent directories should exist.
    """
    p = Path(path)

    if len(p.parts) <= 1:
        return  # Nothing to create for a bare filename/single component

    # Determine whether the last part looks like a file (has an extension).
    if "." in p.parts[-1]:
        dir_path = Path(*p.parts[:-1])
    else:
        dir_path = p

    if not dir_path.exists():
        os.makedirs(dir_path)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def singleton(cls: Type[T]) -> Callable[..., T]:
    """Class decorator that enforces the singleton pattern.

    The first call constructs and caches the instance. If the class defines
    ``__post_init__``, it is invoked immediately after construction. Subsequent
    calls return the cached instance regardless of the arguments passed.

    Args:
        cls: The class to wrap as a singleton.

    Returns:
        A wrapper function that always returns the single shared instance.
    """
    instances: Dict[type, Any] = {}

    @wraps(cls)
    def get_instance(*args: Any, **kwargs: Any) -> T:
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
            if hasattr(instances[cls], "__post_init__"):
                instances[cls].__post_init__()
        return instances[cls]

    return get_instance


# ---------------------------------------------------------------------------
# Profiler
# ---------------------------------------------------------------------------

class Profiler:
    """Lightweight label-based wall-clock profiler.

    Usage::

        profiler = Profiler()
        profiler.start_profile("my_task")
        do_work()
        profiler.end_profile("my_task")  # prints elapsed time if > 1 ms
    """

    def __init__(self) -> None:
        self.start_times: Dict[str, float] = {}

    def start_profile(self, label: str) -> None:
        """Record the start time for *label*.

        Args:
            label: An arbitrary string identifying the profiling session.
        """
        self.start_times[label] = time.time()

    def end_profile(self, label: str) -> None:
        """End the session for *label* and print the elapsed time.

        Elapsed times below 1 ms are suppressed to reduce noise. If no
        matching ``start_profile`` call exists a warning is printed instead.

        Args:
            label: The label passed to the corresponding ``start_profile`` call.
        """
        if label not in self.start_times:
            print(f"No profiling session found for label '{label}'.")
            return

        elapsed = time.time() - self.start_times[label]
        if elapsed > 0.001:
            print(f"Time elapsed for '{label}': {elapsed:.4f} seconds")


# ---------------------------------------------------------------------------
# Class repository (singleton)
# ---------------------------------------------------------------------------

@singleton
class ClassRepository:
    """A singleton registry that maps class names to their types.

    Useful for dynamic instantiation by name — e.g. deserialising objects
    whose concrete type is stored as a string.
    """

    def __init__(self) -> None:
        self.classes: Dict[str, type] = {}

    def add_module_classes(self, globals_dict: Dict[str, Any]) -> None:
        """Discover and register every class defined in *globals_dict*.

        Typically called with ``globals()`` from the module you want to index.
        ``ClassRepository`` itself is excluded to avoid self-registration.

        Args:
            globals_dict: The global namespace to scan (pass ``globals()``).
        """
        for name, obj in globals_dict.items():
            if name != "ClassRepository" and isinstance(obj, type):
                self.classes[name] = obj

    def add_class(self, cls: type) -> None:
        """Register a single class.

        Args:
            cls: The class to register.

        Raises:
            TypeError: If *cls* is not a type.
        """
        if not isinstance(cls, type):
            raise TypeError("'cls' must be a type.")
        self.classes[cls.__name__] = cls

    def get_class(self, class_name: str) -> type | None:
        """Return the class registered under *class_name*, or ``None``.

        Args:
            class_name: The ``__name__`` of the desired class.

        Returns:
            The registered class, or ``None`` if not found.
        """
        return self.classes.get(class_name)

    def list_classes(self) -> List[str]:
        """Return the names of all registered classes.

        Returns:
            A list of class name strings.
        """
        return list(self.classes.keys())


# ---------------------------------------------------------------------------
# Pickle-based persistence
# ---------------------------------------------------------------------------

class PickleStore:
    """Static helpers for persisting arbitrary Python objects via ``pickle``."""

    @staticmethod
    def touch_file(default_obj: Any, filename: str) -> None:
        """Ensure *filename* exists, creating it with *default_obj* if absent.

        Args:
            default_obj: The object to pickle if the file does not yet exist.
            filename: Path to the target pickle file.
        """
        if not Path(filename).exists():
            PickleStore.save_object(default_obj, filename)

    @staticmethod
    def save_object(obj: Any, filename: str) -> None:
        """Serialise *obj* to *filename* using pickle.

        Intermediate directories are created automatically via
        :func:`ensure_dirs_exist`.

        Args:
            obj: The Python object to serialise.
            filename: Destination file path.
        """
        try:
            ensure_dirs_exist(filename)
            with open(filename, "wb") as fh:
                pickle.dump(obj, fh)
        except Exception as exc:
            print(f"Failed to save object to {filename}: {exc}")

    @staticmethod
    def load_object(filename: str) -> Any | None:
        """Deserialise and return the object stored in *filename*.

        Args:
            filename: Path to a pickle file created by :meth:`save_object`.

        Returns:
            The deserialised object, or ``None`` if the file is missing or
            an error occurs.
        """
        try:
            with open(filename, "rb") as fh:
                return pickle.load(fh)
        except FileNotFoundError:
            print(f"No such file: {filename}")
        except Exception as exc:
            print(f"Failed to load object from {filename}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Compression / serialisation
# ---------------------------------------------------------------------------

class DataSerializer:
    """Compress, encode, and JSON-serialise dictionaries whose values are
    strings or nested dicts.

    Compression uses zlib; the compressed bytes are hex-encoded so they can
    be safely embedded in JSON. Serialisation and deserialisation of
    individual keys are parallelised with a ``ProcessPoolExecutor``.

    Supported value types: ``str``, ``dict``.
    """

    @staticmethod
    def compress_text(text: str, level: int = 6) -> str:
        """Compress *text* with zlib and return the result as a hex string.

        Args:
            text: UTF-8 text to compress.
            level: zlib compression level (0–9). Default is 6.

        Returns:
            Hex-encoded compressed bytes.
        """
        compressed = zlib.compress(text.encode("utf-8"), level)
        return compressed.hex()

    @staticmethod
    def decompress_text(hex_encoded: str) -> str:
        """Decompress a hex string produced by :meth:`compress_text`.

        Args:
            hex_encoded: Hex-encoded compressed bytes.

        Returns:
            The original UTF-8 text.
        """
        compressed = bytes.fromhex(hex_encoded)
        return zlib.decompress(compressed).decode("utf-8")

    @staticmethod
    def serialize_dict(data: Dict[str, Any], level: int = 6) -> str:
        """Compress each value in *data* and serialise the result to a JSON string.

        Each value is replaced by a ``(type_tag, compressed_hex)`` tuple so
        that the correct deserialisation path can be chosen later.

        Args:
            data: A flat dictionary whose values are ``str`` or ``dict``.
            level: zlib compression level passed to :meth:`compress_text`.

        Returns:
            A JSON string representing the compressed dictionary.

        Raises:
            ValueError: If a value's type is not ``str`` or ``dict``.
        """
        def compress_entry(key: str, value: Any) -> None:
            if isinstance(value, str):
                data[key] = ("str", DataSerializer.compress_text(value, level))
            elif isinstance(value, dict):
                data[key] = ("dict", DataSerializer.compress_text(json.dumps(value), level))
            else:
                raise ValueError(f"Serialisation not supported for type {type(value)}.")

        with concurrent.futures.ProcessPoolExecutor() as executor:
            futures = [
                executor.submit(compress_entry, key, value)
                for key, value in data.items()
            ]
            concurrent.futures.wait(futures)

        return json.dumps(data)

    @staticmethod
    def deserialize_dict(json_str: str) -> Dict[str, Any]:
        """Deserialise a JSON string produced by :meth:`serialize_dict`.

        Each ``(type_tag, compressed_hex)`` pair is decompressed and
        converted back to its original type.

        Args:
            json_str: A JSON string as returned by :meth:`serialize_dict`.

        Returns:
            The reconstructed dictionary with original value types restored.
        """
        data: Dict[str, Any] = json.loads(json_str)

        def decompress_entry(key: str, value: Any) -> None:
            type_tag, compressed_hex = value
            if type_tag == "str":
                data[key] = DataSerializer.decompress_text(compressed_hex)
            elif type_tag == "dict":
                data[key] = json.loads(DataSerializer.decompress_text(compressed_hex))

        with concurrent.futures.ProcessPoolExecutor() as executor:
            futures = [
                executor.submit(decompress_entry, key, value)
                for key, value in data.items()
            ]
            concurrent.futures.wait(futures)

        return data