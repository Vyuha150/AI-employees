"""Idempotency keys (Part 39).

A key names one logical operation, e.g. ("org_1", "schedule", "daily_brief", "2026-11-20T07:30+05:30").
`claim()` succeeds exactly once per key; a duplicate trigger gets the original record back.
PostgreSQL: table idempotency_keys with PRIMARY KEY (organization_id, key) and
INSERT ... ON CONFLICT DO NOTHING RETURNING.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .common import Clock, utc_now


def idempotency_key(organization_id: str, kind: str, *parts: str) -> str:
    if not organization_id or not kind or not parts:
        raise ValueError("an idempotency key needs organization, kind and at least one discriminator")
    raw = "|".join((organization_id, kind, *parts))
    return f"{kind}:{hashlib.sha256(raw.encode()).hexdigest()[:40]}"


@dataclass(frozen=True)
class IdempotencyRecord:
    organization_id: str
    key: str
    created_at: datetime
    result_ref: str | None = None


class IdempotencyStore(Protocol):
    def claim(self, organization_id: str, key: str, result_ref: str | None = None) -> tuple[bool, IdempotencyRecord]: ...
    def get(self, organization_id: str, key: str) -> IdempotencyRecord | None: ...
    def set_result(self, organization_id: str, key: str, result_ref: str) -> None: ...


class InMemoryIdempotencyStore:
    def __init__(self, clock: Clock = utc_now):
        self._rows: dict[tuple[str, str], IdempotencyRecord] = {}
        self._lock = threading.Lock()
        self.clock = clock

    def claim(self, organization_id: str, key: str, result_ref: str | None = None) -> tuple[bool, IdempotencyRecord]:
        with self._lock:
            existing = self._rows.get((organization_id, key))
            if existing is not None:
                return False, existing
            rec = IdempotencyRecord(organization_id, key, self.clock(), result_ref)
            self._rows[(organization_id, key)] = rec
            return True, rec

    def get(self, organization_id: str, key: str) -> IdempotencyRecord | None:
        return self._rows.get((organization_id, key))

    def set_result(self, organization_id: str, key: str, result_ref: str) -> None:
        with self._lock:
            rec = self._rows[(organization_id, key)]
            self._rows[(organization_id, key)] = IdempotencyRecord(rec.organization_id, rec.key, rec.created_at,
                                                                   result_ref)
