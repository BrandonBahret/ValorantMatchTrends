from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar, Union

from lark import Lark, Transformer

# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------

class UNSET:
    """Sentinel type representing a missing or unresolved value.

    Used instead of None so that None can be a legitimate return value
    from a query.
    """

    _instance: Optional[UNSET] = None

    def __new__(cls) -> UNSET:
        # Singleton — there is only ever one UNSET instance.
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "JsonInjest.UNSET"


# Convenience singleton so callers can write `is UNSET` rather than `== UNSET`.
UNSET = UNSET()

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Query AST nodes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JIPath:
    """A dot-separated key path used to navigate a JSON structure.

    Example: ``"a.b.c"`` → ``JIPath(keys=('a', 'b', 'c'))``
    """

    keys: Tuple[str, ...]


@dataclass(frozen=True)
class JIMatch:
    """A filter expression that keeps only items where a nested key equals a value.

    Example: ``"?type=admin"`` → ``JIMatch(key_path=JIPath(('type',)), value='admin')``
    """

    key_path: JIPath
    value: Any


# ---------------------------------------------------------------------------
# Grammar & transformer
# ---------------------------------------------------------------------------

#: Lark grammar for the selector mini-language.
#:
#: Syntax examples:
#:   ``"users"``                     – navigate to the ``users`` key
#:   ``"users.0.name"``              – navigate nested keys / indices
#:   ``"users?role=admin"``          – filter list items where role == "admin"
_GRAMMAR = r"""
    start:      path_expr match_expr?
    match_expr: "?" path_expr "=" ITEM
    path_expr:  ITEM ("." ITEM)*

    ITEM: CNAME | ESCAPED_STRING | SIGNED_NUMBER

    _STRING_INNER: /[a-zA-Z0-9-]+/
    STRING:        _STRING_INNER /(?<!\\)(\\\\)*?/

    %ignore WS

    %import common.CNAME
    %import common.ESCAPED_STRING
    %import common.SIGNED_NUMBER
    %import common.WS
"""

QueryParser = Lark(_GRAMMAR, parser="lalr")


def _dequote(s: str) -> str:
    """Strip matching single or double quotes from a string token, if present."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


class JIQuery(Transformer):
    """Lark transformer that converts a parse tree into a list of AST nodes."""

    def start(self, children: List[Any]) -> List[Any]:
        return children

    def path_expr(self, children: List[str]) -> JIPath:
        return JIPath(tuple(children))

    def match_expr(self, children: Tuple[JIPath, Any]) -> JIMatch:
        key_path, value = children
        return JIMatch(key_path, value)

    def ESCAPED_STRING(self, token: Any) -> str:  # noqa: N802 – must match Lark terminal name
        return _dequote(str(token))

    def ITEM(self, token: Any) -> str:  # noqa: N802
        return _dequote(str(token))


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class JsonInjester:
    """Lightweight query interface for navigating and filtering JSON data.

    Parameters
    ----------
    json_data:
        Either a raw JSON string or an already-parsed dictionary.
    root:
        Optional dot-separated path to use as the starting cursor.
        For example, ``root="data.users"`` is equivalent to immediately
        calling ``.get("data.users")`` and using that as the new root.
    default_tail:
        When a ``get()`` call resolves to a ``dict``, automatically
        follow this additional selector before returning.  Useful when
        every value in a collection has the same wrapper key.
    """

    def __init__(
        self,
        json_data: Union[str, Dict[str, Any]],
        root: Optional[str] = None,
        default_tail: Optional[str] = None,
    ) -> None:
        self.default_tail = default_tail

        if isinstance(json_data, str):
            self.data: Any = json.loads(json_data)
        elif isinstance(json_data, dict):
            self.data = json_data
        else:
            raise ValueError(
                f"json_data must be a str or dict, got {type(json_data).__name__!r}"
            )

        if root is not None:
            self.data = self._move_cursor(self.data, JIPath(tuple(root.split("."))))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def has(self, selector: str) -> bool:
        """Return ``True`` if the selector resolves to an existing value."""
        return self.get(selector) is not UNSET

    def get(
        self,
        selector: str,
        default_value: Any = UNSET,
        cast: Optional[Type[T]] = None,
    ) -> Any:
        """Evaluate *selector* against the current data and return the result.

        Parameters
        ----------
        selector:
            A dot-separated path, optionally followed by a ``?key=value``
            filter expression.  Examples::

                "name"
                "address.city"
                "users?role=admin"

        default_value:
            Returned when the path does not exist or resolves to ``None``.
            Defaults to ``UNSET`` (the sentinel), which means "no default".
        cast:
            When provided and the resolved value is a ``dict``, the dict is
            passed as keyword arguments to this type/constructor.

        Returns
        -------
        Any
            The resolved value, ``default_value`` if the path is missing,
            or ``UNSET`` if no default was supplied and the path is missing.
        """
        tree = QueryParser.parse(selector)
        actions: List[Union[JIPath, JIMatch]] = JIQuery().transform(tree)

        cursor: Any = self.data
        result: Any = cursor

        for action in actions:
            if isinstance(action, JIPath):
                cursor = self._move_cursor(cursor, action)
                if cursor is UNSET:
                    return default_value
                result = cursor

            elif isinstance(action, JIMatch):
                result = self._apply_filter(cursor, action)

        # Optionally follow a default tail selector when the result is a dict.
        if isinstance(result, dict) and self.default_tail:
            result = JsonInjester(result).get(self.default_tail)

        # Optionally cast a dict result to the requested type.
        if cast is not None and isinstance(result, dict):
            result = cast(result)

        # Fall back to default_value when the resolved result is None.
        if result is None and default_value is not UNSET:
            return default_value

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _move_cursor(self, cursor: Any, path: JIPath) -> Any:
        """Walk *cursor* along each key in *path*.

        Returns ``UNSET`` if any key is absent or if an intermediate node
        is not a dict.
        """
        for key in path.keys:
            if not isinstance(cursor, dict):
                raise AttributeError(
                    f"Expected a dict while navigating key {key!r}, "
                    f"got {type(cursor).__name__!r}"
                )
            if key not in cursor:
                return UNSET
            cursor = cursor[key]
        return cursor

    def _apply_filter(
        self,
        cursor: Any,
        match: JIMatch,
    ) -> List[Any]:
        """Return the subset of *cursor* items that satisfy *match*.

        Handles two container shapes:

        * **list of dicts** – each element is checked directly.
        * **dict of dicts** – each ``(key, value)`` pair is checked;
          matching pairs are returned as ``(key, value)`` tuples.
        """
        results: List[Any] = []

        for item in cursor:
            try:
                if isinstance(item, dict):
                    # cursor is a list; item is one element.
                    test_value = self._move_cursor(item, match.key_path)
                    if test_value == match.value:
                        results.append(item)
                elif isinstance(item, str):
                    # cursor is a dict; item is a key string.
                    test_value = self._move_cursor(cursor[item], match.key_path)
                    if test_value == match.value:
                        results.append((item, cursor[item]))
            except KeyError:
                # Key absent in this item — skip silently.
                pass

        return results