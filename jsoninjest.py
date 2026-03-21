from typing import Dict, Union
import json

from lark import Lark, Transformer, v_args
from dataclasses import dataclass


class UNSET:
    '''Sentinal for values are unset'''
    def __repr__(self):
        return 'JsonInjest.UNSET'

@dataclass
class JIPath:
    keys: tuple

@dataclass
class JIMatch:
    key_path: JIPath
    value: object

def dequote(s):
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    return s

class JIQuery(Transformer):
    def start(self, children):
        return children

    def path_expr(self, children):
        return JIPath(tuple(children))

    def match_expr(self, children):
        key, value = children
        return JIMatch(key, value)

    def ESCAPED_STRING(self, token):
        return dequote(str(token))

    def ITEM(self, token):
        return dequote(str(token))

QueryParser = Lark(r"""
    start: path_expr [match_expr]
    match_expr: "?" path_expr "=" ITEM
    path_expr: ITEM ("." ITEM)*

    ITEM: CNAME | ESCAPED_STRING | SIGNED_NUMBER

    _STRING_INNER: /[a-zA-Z0-9-]+/
    STRING: _STRING_INNER /(?<!\\)(\\\\)*?/

    %ignore WS

    %import common.CNAME
    %import common.ESCAPED_STRING
    %import common.SIGNED_NUMBER
    %import common.WS
""", parser='lalr')

class JsonInjester:
    def __init__(self, json_data: Union[str, Dict], root: str = None, default_tail=None):
        self.query_parser = QueryParser
        self.default_tail = default_tail

        if isinstance(json_data, str):
            self.data = json.loads(json_data)
        elif isinstance(json_data, dict):
            self.data = json_data
        else:
            raise ValueError(f"json_data must be a string or a dictionary (not {json_data!r})")
        
        if root is not None:
            self.data = self.move_cursor(self.data, JIPath(root.split('.')))
    
    def move_cursor(self, cursor, path: JIPath):
        for key in path.keys:
            if not isinstance(cursor, dict):
                raise AttributeError(f"cursor {cursor} is not dictionary.")
            
            if key in cursor.keys():
                cursor = cursor[key]
            else:
                return UNSET
        
        return cursor
    
    def has(self, selector: str):
        return self.get(selector) is not UNSET

    def get(self, selector: str, default_value=UNSET, cast=None):
        tree = QueryParser.parse(selector)
        selector = JIQuery().transform(tree)
        cursor = self.data
        result = cursor

        for action in selector:
            if isinstance(action, JIPath):
                cursor = self.move_cursor(cursor, action)
                if cursor == UNSET:
                    return default_value
                else:
                    result = cursor
            
            if isinstance(action, JIMatch):
                result = []

                for each in cursor:
                    try:
                        if isinstance(each, dict):
                            test_point = self.move_cursor(each, action.key_path)
                            
                            if test_point == action.value:
                                result.append(each)
                        elif isinstance(each, str):
                            key = each
                            test_point = self.move_cursor(cursor[key], action.key_path)
                            
                            if test_point == action.value:
                                result.append((key, cursor[key]))
                    except KeyError as e:
                        pass
            
        if isinstance(result, dict) and self.default_tail:
            result = JsonInjester(result).get(self.default_tail)
        
        if isinstance(cast, type) and isinstance(result, dict):
            result = cast(result)
        
        if result is None and default_value is not UNSET:
            return default_value
        
        return result