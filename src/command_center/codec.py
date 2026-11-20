"""Type-driven JSON codec for domain dataclasses (used by the PostgreSQL adapters).

encode(record) -> JSON-ready dict; decode(cls, data) -> record, guided by the dataclass's type
hints (Enum, datetime, date, Decimal, tuple/frozenset/list, Mapping, Optional, nested
dataclasses). Decimal is encoded as a string to keep exact values.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any, Mapping


def encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(encode(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


@lru_cache(maxsize=None)
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _decode(tp: Any, v: Any) -> Any:
    if v is None or tp is Any:
        return v
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        last_err: Exception | None = None
        for a in non_none:
            try:
                return _decode(a, v)
            except (TypeError, ValueError, KeyError) as exc:
                last_err = exc
        raise ValueError(f"cannot decode {v!r} as {tp}") from last_err
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], x) for x in v)
        if args:
            return tuple(_decode(a, x) for a, x in zip(args, v))
        return tuple(v)
    if origin in (frozenset, set):
        return frozenset(_decode(args[0], x) for x in v) if args else frozenset(v)
    if origin is list:
        return [_decode(args[0], x) for x in v] if args else list(v)
    if origin in (dict, Mapping, typing.Mapping) or (origin is not None and isinstance(origin, type)
                                                     and issubclass(origin, Mapping)):
        return {k: _decode(args[1], x) for k, x in v.items()} if len(args) == 2 else dict(v)
    if isinstance(tp, type):
        if dataclasses.is_dataclass(tp):
            return decode(tp, v)
        if issubclass(tp, Enum):
            return tp(v)
        if tp is datetime:
            return datetime.fromisoformat(v)
        if tp is date:
            return date.fromisoformat(v)
        if tp is Decimal:
            return Decimal(str(v))
        if tp is float:
            return float(v)
        if tp is int and not isinstance(v, bool):
            return int(v)
    return v


def decode(cls: type, data: Mapping[str, Any]) -> Any:
    hints = _hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            kwargs[f.name] = _decode(hints.get(f.name, Any), data[f.name])
    return cls(**kwargs)
