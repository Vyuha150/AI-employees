"""Deterministic canonical serialization and hashing (payload integrity, Part 7).

Rules:
- object keys sorted, no insignificant whitespace, UTF-8;
- Decimal and float serialized from their exact decimal string; integral floats are NOT
  coerced to ints (10000.0 and 10000 hash differently, so the approved payload must be
  presented in exactly the same form);
- NaN / Infinity are rejected (they have no canonical meaning);
- datetimes must be timezone-aware and are serialized as UTC ISO-8601;
- tuples are treated as lists; sets are rejected (no stable order);
- unsupported types are rejected rather than stringified.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

HASH_ALGORITHM = "sha256"


class _Raw(str):
    """Marker for pre-rendered JSON number literals."""


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/Infinity cannot be canonicalized")
        return _Raw(repr(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite Decimal cannot be canonicalized")
        return {"$decimal": format(value.normalize(), "f")}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be canonicalized")
        return {"$datetime": value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"mapping keys must be strings, got {type(k).__name__}")
            out[k] = _normalize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    raise ValueError(f"type {type(value).__name__} cannot be canonicalized")


def _encode(value: Any) -> str:
    if isinstance(value, _Raw):
        return str(value)
    if isinstance(value, dict):
        items = sorted(value.items())
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _encode(v) for k, v in items) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def canonical_json(value: Any) -> str:
    return _encode(_normalize(value))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def action_binding_hash(
    *,
    organization_id: str,
    agent_id: str,
    run_id: str,
    tool_name: str,
    action_type: str,
    payload: Mapping[str, Any],
) -> str:
    """Hash binding an approval to one exact action: org + agent + run + tool + action type + payload.

    Every payload field is treated as material. A more permissive "material fields only"
    policy would need an explicit per-tool allow-list; until one exists, any change at all
    requires a new approval (fail closed).
    """
    for label, v in (("organization_id", organization_id), ("agent_id", agent_id), ("run_id", run_id),
                     ("tool_name", tool_name), ("action_type", action_type)):
        if not v:
            raise ValueError(f"{label} is required for an action binding")
    return canonical_hash({
        "v": 1,
        "organization_id": organization_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "tool_name": tool_name,
        "action_type": action_type,
        "payload": payload,
    })
