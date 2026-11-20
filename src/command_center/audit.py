"""Append-only audit event system (Part 8).

Design
- `AuditEvent` is immutable; payload/state are stored as canonical JSON text and
  returned as fresh copies, so callers cannot mutate history through references.
- Each stored event carries `sequence`, `prev_hash` and `event_hash` (SHA-256 over the
  canonical event body + prev_hash). `verify_chain()` detects edits, deletions or reordering.
- `AuditStore` is the storage contract. PostgreSQL mapping (later):
    audit_events(sequence BIGSERIAL PK, event_id UUID UNIQUE, ..., prev_hash, event_hash)
    + REVOKE UPDATE, DELETE from the application role
    + a BEFORE UPDATE OR DELETE trigger that raises.
  Appends are serialized per organization (advisory lock) so the chain stays linear.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .canonical import canonical_hash
from .common import ActorType, Clock, StrEnum, new_id, utc_now
from .errors import AuditIntegrityError

GENESIS_HASH = "0" * 64


class EventType(StrEnum):
    AGENT_RUN_CREATED = "agent_run.created"
    AGENT_RUN_STARTED = "agent_run.started"
    AGENT_RUN_STATE_CHANGED = "agent_run.state_changed"
    AGENT_RUN_COMPLETED = "agent_run.completed"
    AGENT_RUN_FAILED = "agent_run.failed"
    AGENT_RUN_CANCELLED = "agent_run.cancelled"
    AGENT_RUN_USAGE_RECORDED = "agent_run.usage_recorded"

    TOOL_REQUESTED = "tool.requested"
    TOOL_EXECUTED = "tool.executed"
    TOOL_FAILED = "tool.failed"
    TOOL_DENIED = "tool.denied"
    TOOL_EXECUTION_UNKNOWN = "tool.execution_unknown"
    TOOL_RETRIED = "tool.retried"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_CANCELLED = "approval.cancelled"
    APPROVAL_EXECUTION_STARTED = "approval.execution_started"
    APPROVAL_EXECUTED = "approval.executed"
    APPROVAL_EXECUTION_FAILED = "approval.execution_failed"
    APPROVAL_EXECUTION_UNKNOWN = "approval.execution_unknown"
    APPROVAL_RECONCILED = "approval.reconciled"

    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"

    RECOMMENDATION_CREATED = "recommendation.created"
    RECOMMENDATION_UPDATED = "recommendation.updated"
    RECOMMENDATION_APPROVED = "recommendation.approved"
    RECOMMENDATION_REJECTED = "recommendation.rejected"

    CHANGE_REQUEST_CREATED = "change_request.created"
    CHANGE_REQUEST_UPDATED = "change_request.updated"
    OUTCOME_RECORDED = "outcome.recorded"

    OUTPUT_CREATED = "output.created"
    OUTPUT_REVIEWED = "output.reviewed"

    MEMORY_PROPOSED = "memory.proposed"
    MEMORY_ACCEPTED = "memory.accepted"
    MEMORY_REJECTED = "memory.rejected"
    MEMORY_INVALIDATED = "memory.invalidated"
    MEMORY_EXPIRED = "memory.expired"

    DELEGATION_CREATED = "delegation.created"
    DELEGATION_UPDATED = "delegation.updated"

    ALERT_CREATED = "alert.created"
    ALERT_ACKNOWLEDGED = "alert.acknowledged"
    ALERT_RESOLVED = "alert.resolved"
    ALERT_DISMISSED = "alert.dismissed"
    NOTIFICATION_CREATED = "notification.created"
    ESCALATION_TRIGGERED = "escalation.triggered"
    KPI_OBSERVATION_RECORDED = "kpi.observation_recorded"
    MEMORY_REVIEW_DUE = "memory.review_due"
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXCEEDED = "budget.exceeded"
    BUDGET_OVERRIDE_GRANTED = "budget.override_granted"
    MODEL_CALLED = "model.called"
    MODEL_FAILED = "model.failed"
    MODEL_FALLBACK_USED = "model.fallback_used"
    USAGE_RECORDED = "usage.recorded"
    SCHEDULE_TRIGGERED = "schedule.triggered"
    SCHEDULE_SKIPPED = "schedule.skipped"
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_STEP = "workflow.step"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    WORKFLOW_WAITING = "workflow.waiting"
    WORKFLOW_DEFINITION_CHANGED = "workflow.definition_changed"
    COMMAND_RECEIVED = "command.received"
    ATTACHMENT_REJECTED = "attachment.rejected"
    KNOWLEDGE_INGESTED = "knowledge.ingested"
    CONTEXT_ASSEMBLED = "context.assembled"
    CONFIGURATION_CHANGED = "configuration.changed"
    RISK_OVERRIDDEN_BY_POLICY = "risk.overridden_by_policy"
    RISK_DECLARED_HIGHER_THAN_POLICY = "risk.declared_higher_than_policy"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(value)
    raise TypeError(f"{type(value).__name__} is not audit-serializable")


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=_json_default, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    timestamp: datetime
    organization_id: str
    actor_type: ActorType
    actor_id: str
    agent_id: str | None
    run_id: str | None
    event_type: EventType
    object_type: str
    object_id: str
    payload_json: str | None = None
    previous_state: str | None = None
    new_state: str | None = None
    sequence: int | None = None
    prev_hash: str | None = None
    event_hash: str | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json) if self.payload_json else {}

    def body(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "organization_id": self.organization_id,
            "actor_type": self.actor_type.value,
            "actor_id": self.actor_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "event_type": self.event_type.value,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "payload_json": self.payload_json,
            "previous_state": self.previous_state,
            "new_state": self.new_state,
            "sequence": self.sequence,
        }

    def to_record(self) -> dict[str, Any]:
        return {**self.body(), "prev_hash": self.prev_hash, "event_hash": self.event_hash}

    @classmethod
    def from_record(cls, rec: Mapping[str, Any]) -> "AuditEvent":
        return cls(
            event_id=rec["event_id"],
            timestamp=datetime.fromisoformat(rec["timestamp"]),
            organization_id=rec["organization_id"],
            actor_type=ActorType(rec["actor_type"]),
            actor_id=rec["actor_id"],
            agent_id=rec["agent_id"],
            run_id=rec["run_id"],
            event_type=EventType(rec["event_type"]),
            object_type=rec["object_type"],
            object_id=rec["object_id"],
            payload_json=rec["payload_json"],
            previous_state=rec["previous_state"],
            new_state=rec["new_state"],
            sequence=rec["sequence"],
            prev_hash=rec["prev_hash"],
            event_hash=rec["event_hash"],
        )


def _chain_hash(event: AuditEvent, prev_hash: str) -> str:
    return canonical_hash({"body": event.body(), "prev_hash": prev_hash})


class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> AuditEvent: ...
    def events(self, **filters: Any) -> list[AuditEvent]: ...
    def verify_chain(self) -> None: ...


def _matches(e: AuditEvent, filters: Mapping[str, Any]) -> bool:
    for key, want in filters.items():
        if want is None:
            continue
        have = getattr(e, key)
        if isinstance(want, (list, tuple, set, frozenset)):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


class InMemoryAuditStore:
    """Append-only in-memory store. There is intentionally no update or delete method."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def _seal(self, event: AuditEvent) -> AuditEvent:
        if event.sequence is not None or event.event_hash is not None:
            raise AuditIntegrityError("events are sealed by the store; do not pre-set sequence/hash")
        prev = self._events[-1].event_hash if self._events else GENESIS_HASH
        seq = len(self._events) + 1
        staged = AuditEvent(**{**event.__dict__, "sequence": seq})
        return AuditEvent(**{**staged.__dict__, "prev_hash": prev, "event_hash": _chain_hash(staged, prev)})

    def append(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            sealed = self._seal(event)
            self._persist(sealed)
            self._events.append(sealed)
            return sealed

    def _persist(self, event: AuditEvent) -> None:  # hook for file-backed subclass
        pass

    def events(self, **filters: Any) -> list[AuditEvent]:
        with self._lock:
            snapshot = list(self._events)
        return [e for e in snapshot if _matches(e, filters)]

    def verify_chain(self) -> None:
        with self._lock:
            snapshot = list(self._events)
        verify_events(snapshot)


def verify_events(events: Iterable[AuditEvent]) -> None:
    prev = GENESIS_HASH
    for i, e in enumerate(events, start=1):
        if e.sequence != i:
            raise AuditIntegrityError(f"sequence gap at position {i} (found {e.sequence})")
        if e.prev_hash != prev:
            raise AuditIntegrityError(f"event {e.event_id}: prev_hash does not link to previous event")
        unsealed = AuditEvent(**{**e.__dict__, "prev_hash": None, "event_hash": None})
        if _chain_hash(unsealed, prev) != e.event_hash:
            raise AuditIntegrityError(f"event {e.event_id}: hash mismatch (record altered)")
        prev = e.event_hash


class JsonlAuditStore(InMemoryAuditStore):
    """File-backed append-only store (one JSON record per line, fsync on append).
    The existing file is loaded and its chain verified at start-up; a broken chain refuses to open."""

    def __init__(self, path: str | os.PathLike[str]):
        super().__init__()
        self._path = Path(path)
        if self._path.exists():
            with self._path.open("r", encoding="utf-8") as fh:
                loaded = [AuditEvent.from_record(json.loads(line)) for line in fh if line.strip()]
            verify_events(loaded)
            self._events = loaded

    def _persist(self, event: AuditEvent) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_record(), sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


class AuditLogger:
    """Convenience front-end used by every service. Services never write events directly."""

    def __init__(self, store: AuditStore, clock: Clock = utc_now):
        self.store = store
        self.clock = clock
        # Called with each sealed event after it is appended (e.g. the realtime bus). A failing
        # listener never affects the audit write or the business operation.
        self.listeners: list[Any] = []

    def record(
        self,
        event_type: EventType,
        *,
        organization_id: str,
        actor_type: ActorType,
        actor_id: str,
        object_type: str,
        object_id: str,
        agent_id: str | None = None,
        run_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        previous_state: Any = None,
        new_state: Any = None,
    ) -> AuditEvent:
        def state(v: Any) -> str | None:
            if v is None:
                return None
            if isinstance(v, Enum):
                return str(v.value)
            return str(v)

        sealed = self.store.append(AuditEvent(
            event_id=new_id("evt"),
            timestamp=self.clock(),
            organization_id=organization_id,
            actor_type=actor_type,
            actor_id=actor_id,
            agent_id=agent_id,
            run_id=run_id,
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            payload_json=_dump(dict(payload)) if payload else None,
            previous_state=state(previous_state),
            new_state=state(new_state),
        ))
        for listener in list(self.listeners):
            try:
                listener(sealed)
            except Exception:
                pass
        return sealed
