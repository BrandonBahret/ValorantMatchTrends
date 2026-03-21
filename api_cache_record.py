import inspect
import math
import time
from typing import Optional
from utils import ClassRepository


# Sentinel object used to distinguish "not yet resolved" from None in lazy properties.
UNSET = object()

# Maps primitive type name strings to their corresponding Python types.
# Used when deserializing a cache record's 'cast' field.
PRIMITIVE_TYPES_MAP = {
    'bool': bool,
    'bytearray': bytearray,
    'bytes': bytes,
    'complex': complex,
    'dict': dict,
    'float': float,
    'frozenset': frozenset,
    'int': int,
    'list': list,
    'object': object,
    'set': set,
    'str': str,
    'tuple': tuple,
    'type': type,
}


def look_up_class(class_name: str) -> type:
    """Resolve a class by name, checking primitives first then the class repository.

    Args:
        class_name: The name of the class to look up.

    Returns:
        The resolved class type.

    Raises:
        NameError: If the class name is not registered in the repository.
        TypeError: If the resolved object is not a class.
    """
    if class_name in PRIMITIVE_TYPES_MAP:
        return PRIMITIVE_TYPES_MAP[class_name]

    classes = ClassRepository()
    if class_name not in classes.list_classes():
        raise NameError(f'{class_name!r} is not defined')

    cls = classes.get_class(class_name)
    if inspect.isclass(cls):
        return cls

    raise TypeError(f"Class '{class_name}' not found or is not a class")


class CacheRecord:
    """Represents a single cached API response with expiry and optional type casting.

    Records are stored and serialized as plain dicts, with `math.inf` represented
    as the string 'math.inf' to support JSON-safe serialization.
    """

    def __init__(self, record: dict):
        """Initialize a CacheRecord from a raw record dict.

        Args:
            record: A dict with keys: 'timestamp', 'expiry', 'data', and optionally 'cast'.
        """
        self.__record_dict = record
        self.timestamp: float = record['timestamp']
        self.data: dict = record['data']

        # Deserialize 'math.inf' string back to float infinity.
        self.expiry: float = math.inf if record['expiry'] == 'math.inf' else record['expiry']

        # Store the cast class name string; the actual type is resolved lazily.
        self.cast_str: Optional[str] = record.get('cast')
        self.__cast: Optional[type] = UNSET  # Unresolved until first access.

    @staticmethod
    def from_data(data: dict, expiry: float = math.inf, cast: type = None) -> 'CacheRecord':
        """Construct a new CacheRecord from raw data.

        Args:
            data: The payload to cache.
            expiry: Seconds until the record is considered stale. Defaults to never expiring.
            cast: Optional type to cast the data to on retrieval.

        Returns:
            A new CacheRecord instance.
        """
        record = {
            'cast': cast.__name__ if isinstance(cast, type) else None,
            'expiry': expiry,
            'timestamp': time.time(),
            'data': data,
        }
        return CacheRecord(record)

    @property
    def cast(self) -> Optional[type]:
        """Lazily resolve the cast type from its stored class name string.

        Returns None if no cast was specified.
        """
        if self.__cast is UNSET:
            self.__cast = look_up_class(self.cast_str) if isinstance(self.cast_str, str) else None
        return self.__cast

    @property
    def should_convert_type(self) -> bool:
        """True if a valid cast type is set and data should be converted on retrieval."""
        return isinstance(self.cast, type)

    @property
    def is_data_stale(self) -> bool:
        """True if the record has lived past its expiry window."""
        return time.time() > self.timestamp + self.expiry

    def update(self, data: dict):
        """Replace the cached data and refresh the timestamp.

        Args:
            data: The new payload to store.
        """
        self.data = data
        self.timestamp = time.time()
        self.__record_dict['data'] = data
        self.__record_dict['timestamp'] = self.timestamp

    def as_dict(self) -> dict:
        """Serialize the record to a plain dict, encoding infinity as 'math.inf'.

        Returns:
            A JSON-safe dict representation of the record.
        """
        return {
            k: ('math.inf' if v == math.inf else v)
            for k, v in self.__record_dict.items()
        }

    def __repr__(self) -> str:
        label = 'data_stale' if self.is_data_stale else 'data_fresh'
        return f'<{self.cast_str}::{label}>'