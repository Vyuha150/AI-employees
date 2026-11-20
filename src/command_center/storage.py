"""Persistence contracts (Part 9 of the brief: agent output persistence and entity storage).

`Repository` is the contract a PostgreSQL adapter implements later:
  - one table per record type, primary key = `id_field`;
  - `version` column for optimistic concurrency (UPDATE ... WHERE id = $1 AND version = $2);
  - records are immutable dataclasses; an update stores a new snapshot, and the history
    of state changes lives in the append-only audit log.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import replace
from typing import Any, Callable, Generic, Iterable, Mapping, Protocol, TypeVar

from .errors import ConcurrencyError, NotFoundError

T = TypeVar("T")


MAX_PAGE = 500


def clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_PAGE))


def _sortable(v: Any) -> Any:
    return getattr(v, "value", v)


def matches_filters(record: Any, filters: Mapping[str, Any]) -> bool:
    """Filter syntax shared by repositories: {"field": v, "field__in": [...], "field__gt": v,
    "field__gte": v, "field__lt": v, "field__lte": v}. Enum values compare by value."""
    for key, want in filters.items():
        name, _, op = key.partition("__")
        if op not in ("", "in", "gt", "gte", "lt", "lte"):
            raise ValueError(f"unsupported filter operator {op!r}")
        have = _sortable(getattr(record, name))
        if op in ("in",):
            if have not in {_sortable(w) for w in want}:
                return False
            continue
        want = _sortable(want)
        if op == "" and have != want:
            return False
        if op and have is None:
            return False
        if (op == "gt" and not have > want) or (op == "gte" and not have >= want) \
                or (op == "lt" and not have < want) or (op == "lte" and not have <= want):
            return False
    return True


class Repository(Protocol[T]):
    def add(self, record: T) -> T: ...
    def get(self, record_id: str) -> T: ...
    def find(self, record_id: str) -> T | None: ...
    def update(self, record: T, expected_version: int) -> T: ...
    def list(self, predicate: Callable[[T], bool] | None = None) -> list[T]: ...


class InMemoryRepository(Generic[T]):
    """Thread-safe in-memory repository. Records must be frozen dataclasses with a `version` field."""

    def __init__(self, id_field: str):
        self._id_field = id_field
        self._rows: dict[str, T] = {}
        self._lock = threading.RLock()

    def _id(self, record: T) -> str:
        return getattr(record, self._id_field)

    def add(self, record: T) -> T:
        with self._lock:
            rid = self._id(record)
            if rid in self._rows:
                raise ConcurrencyError(f"{self._id_field} {rid!r} already exists")
            self._rows[rid] = record
            return record

    def get(self, record_id: str) -> T:
        found = self.find(record_id)
        if found is None:
            raise NotFoundError(f"{self._id_field} {record_id!r} not found")
        return found

    def find(self, record_id: str) -> T | None:
        with self._lock:
            return self._rows.get(record_id)

    def update(self, record: T, expected_version: int) -> T:
        with self._lock:
            rid = self._id(record)
            current = self._rows.get(rid)
            if current is None:
                raise NotFoundError(f"{self._id_field} {rid!r} not found")
            if getattr(current, "version") != expected_version:
                raise ConcurrencyError(
                    f"{self._id_field} {rid!r}: expected version {expected_version}, found {getattr(current, 'version')}"
                )
            stored = replace(record, version=expected_version + 1)
            self._rows[rid] = stored
            return stored

    def list(self, predicate: Callable[[T], bool] | None = None) -> list[T]:
        with self._lock:
            rows: Iterable[T] = list(self._rows.values())
        return [r for r in rows if predicate is None or predicate(r)]

    def query(self, filters: Mapping[str, Any] | None = None, *, order_by: str | None = None,
              limit: int = 50, offset: int = 0) -> list[T]:
        """Same contract as the PostgreSQL implementation (see storage.Query)."""
        with self._lock:
            rows = list(self._rows.values())
        rows = [r for r in rows if matches_filters(r, filters or {})]
        if order_by:
            key = order_by.lstrip("-")
            rows.sort(key=lambda r: (_sortable(getattr(r, key)), getattr(r, self._id_field)),
                      reverse=order_by.startswith("-"))
        return rows[offset:offset + clamp_limit(limit)]


def frozen_copy(value):
    """Deep copy used when storing caller-supplied mutable payloads inside immutable records."""
    return copy.deepcopy(value)
