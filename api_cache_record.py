import inspect
import math
import time
from typing import Optional
from utils import ClassRepository


UNSET = object()
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
    'type': type
}
        
def look_up_class(class_name: str) -> type:
    if class_name in PRIMITIVE_TYPES_MAP:
        return PRIMITIVE_TYPES_MAP[class_name]
    
    classes = ClassRepository()
    if class_name not in classes.list_classes():
        raise NameError(f'{class_name!r} is not defined')
    
    cls = classes.get_class(class_name)
    if inspect.isclass(cls):
        return cls
    else:
        raise TypeError(f"Class '{class_name}' not found or is not a class")

class CacheRecord:
    def __init__(self, record: dict):
        substitute_inf = lambda v: math.inf if v == 'math.inf' else v
        self.__record_dict = record
        self.timestamp: float = record['timestamp']
        self.expiry: float = substitute_inf(record['expiry'])
        self.data: dict = record['data']
        
        self.cast_str = record.get('cast')
        self.__cast: Optional[type] = UNSET
    
    @staticmethod
    def from_data(data: dict, expiry: int=math.inf, cast: type=None) -> 'CacheRecord':
        new_record = {
            'cast': None,
            'expiry': expiry,
            'timestamp': time.time(),
            'data': data
        }
        if isinstance(cast, type):
            new_record['cast'] = cast.__name__
        
        return CacheRecord(new_record)
        
    @property
    def cast(self):
        if self.__cast == UNSET:
            if isinstance(self.cast_str, str):
                self.__cast = look_up_class(self.cast_str)
            else:
                self.__cast = None
        
        return self.__cast
    
    @property
    def should_convert_type(self):
        return isinstance(self.cast, type)
    
    def update(self, data: dict):
        self.__record_dict['data'] = data
        self.__record_dict['timestamp'] = time.time()
        self.data = data
        self.timestamp = time.time()
    
    def as_dict(self):
        substitute_inf = lambda v: 'math.inf' if v == math.inf else v
        modified = {
            k: substitute_inf(v) for (k,v) in self.__record_dict.items()
        }
        return modified
    
    @property
    def is_data_stale(self):
        return time.time() > self.timestamp + self.expiry
    
    # def __repr__(self):
    #     label = 'data_stale' if self.is_data_stale else 'data_fresh'
    #     return f'<{self.cast}::{label}>'
