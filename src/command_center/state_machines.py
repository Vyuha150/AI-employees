"""Explicit state machines (Parts 9-12, 17, 18).

Each machine is a closed transition table. Anything not listed is forbidden. Terminal
states have no outgoing transitions; a retry is a new record (a new run, a new approval).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Mapping, TypeVar

from .common import StrEnum
from .errors import InvalidTransitionError

S = TypeVar("S", bound=Enum)


@dataclass(frozen=True)
class StateMachine(Generic[S]):
    name: str
    transitions: Mapping[S, frozenset[S]]

    def allowed(self, current: S) -> frozenset[S]:
        return self.transitions.get(current, frozenset())

    def can(self, current: S, target: S) -> bool:
        return target in self.allowed(current)

    def check(self, current: S, target: S) -> None:
        if not self.can(current, target):
            raise InvalidTransitionError(f"{self.name}: {current.value} -> {target.value} is not permitted")

    def is_terminal(self, state: S) -> bool:
        return not self.allowed(state)


def _machine(name: str, table: Mapping[S, set[S]], states: type[S]) -> StateMachine[S]:
    full = {s: frozenset(table.get(s, set())) for s in states}
    return StateMachine(name, full)


# --- agent run (Part 9) -----------------------------------------------------------
class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    TOOL_WAIT = "TOOL_WAIT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


RUN_MACHINE = _machine("agent_run", {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {RunStatus.TOOL_WAIT, RunStatus.APPROVAL_REQUIRED, RunStatus.SUCCEEDED,
                        RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.TOOL_WAIT: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.APPROVAL_REQUIRED: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
}, RunStatus)


# --- task (Part 10) ---------------------------------------------------------------
class TaskStatus(StrEnum):
    BACKLOG = "BACKLOG"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_AGENT = "WAITING_AGENT"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


_ACTIVE_TASK = {TaskStatus.IN_PROGRESS, TaskStatus.WAITING_HUMAN, TaskStatus.WAITING_AGENT, TaskStatus.BLOCKED}
TASK_MACHINE = _machine("task", {
    TaskStatus.BACKLOG: {TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.QUEUED: {TaskStatus.BACKLOG, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {TaskStatus.WAITING_HUMAN, TaskStatus.WAITING_AGENT, TaskStatus.BLOCKED,
                             TaskStatus.COMPLETED, TaskStatus.CANCELLED},
    TaskStatus.WAITING_HUMAN: {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.WAITING_AGENT: {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
}, TaskStatus)


# --- recommendation (Part 11) -----------------------------------------------------
class RecommendationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    IMPLEMENTATION = "IMPLEMENTATION"
    IMPLEMENTED = "IMPLEMENTED"
    IMPACT_REVIEW = "IMPACT_REVIEW"
    VERIFIED = "VERIFIED"
    ROLLED_BACK = "ROLLED_BACK"


R = RecommendationStatus
RECOMMENDATION_MACHINE = _machine("recommendation", {
    R.PROPOSED: {R.UNDER_REVIEW, R.REJECTED},
    R.UNDER_REVIEW: {R.APPROVED, R.REJECTED},
    R.APPROVED: {R.IMPLEMENTATION},
    R.IMPLEMENTATION: {R.IMPLEMENTED, R.ROLLED_BACK},
    R.IMPLEMENTED: {R.IMPACT_REVIEW, R.ROLLED_BACK},
    R.IMPACT_REVIEW: {R.VERIFIED, R.ROLLED_BACK},
}, RecommendationStatus)
HUMAN_ONLY_RECOMMENDATION_TARGETS = frozenset({R.APPROVED, R.REJECTED, R.VERIFIED})


# --- change request (Part 12) -----------------------------------------------------
class ChangeStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PLANNING = "PLANNING"
    IMPLEMENTATION = "IMPLEMENTATION"
    TESTING = "TESTING"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    IMPACT_MEASUREMENT = "IMPACT_MEASUREMENT"
    CLOSED = "CLOSED"
    ROLLED_BACK = "ROLLED_BACK"


C = ChangeStatus
CHANGE_MACHINE = _machine("change_request", {
    C.DRAFT: {C.APPROVED},
    C.APPROVED: {C.PLANNING},
    C.PLANNING: {C.IMPLEMENTATION},
    C.IMPLEMENTATION: {C.TESTING, C.ROLLED_BACK},
    C.TESTING: {C.REVIEW, C.IMPLEMENTATION, C.ROLLED_BACK},
    C.REVIEW: {C.COMPLETED, C.IMPLEMENTATION, C.ROLLED_BACK},
    C.COMPLETED: {C.IMPACT_MEASUREMENT, C.ROLLED_BACK},
    C.IMPACT_MEASUREMENT: {C.CLOSED, C.ROLLED_BACK},
}, ChangeStatus)
HUMAN_ONLY_CHANGE_TARGETS = frozenset({C.APPROVED, C.CLOSED, C.ROLLED_BACK})


# --- approval (Part 6) ------------------------------------------------------------
class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"   # outcome undetermined; needs human reconciliation
    OUTCOME_UNKNOWN = "EXECUTION_UNKNOWN"     # alias (Part 55 name); same state, same stored value


A = ApprovalStatus
APPROVAL_MACHINE = _machine("approval", {
    A.PENDING: {A.APPROVED, A.REJECTED, A.EXPIRED, A.CANCELLED},
    A.APPROVED: {A.EXECUTED, A.FAILED, A.EXPIRED, A.CANCELLED, A.EXECUTION_UNKNOWN},
    A.EXECUTION_UNKNOWN: {A.EXECUTED, A.FAILED},   # reconciliation only; never re-executed
}, ApprovalStatus)


# --- memory (Part 17) -------------------------------------------------------------
class MemoryStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


MEMORY_MACHINE = _machine("memory", {
    MemoryStatus.PROPOSED: {MemoryStatus.ACCEPTED, MemoryStatus.REJECTED, MemoryStatus.EXPIRED},
    MemoryStatus.ACCEPTED: {MemoryStatus.INVALIDATED, MemoryStatus.EXPIRED},
}, MemoryStatus)


# --- alert (Part 41) ---------------------------------------------------------------
class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


ALERT_MACHINE = _machine("alert", {
    AlertStatus.OPEN: {AlertStatus.ACKNOWLEDGED, AlertStatus.RESOLVED, AlertStatus.DISMISSED},
    AlertStatus.ACKNOWLEDGED: {AlertStatus.RESOLVED, AlertStatus.DISMISSED},
}, AlertStatus)


# --- delegation (Part 18) ---------------------------------------------------------
class DelegationStatus(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


DELEGATION_MACHINE = _machine("delegation", {
    DelegationStatus.REQUESTED: {DelegationStatus.RUNNING, DelegationStatus.CANCELLED},
    DelegationStatus.RUNNING: {DelegationStatus.COMPLETED, DelegationStatus.FAILED, DelegationStatus.CANCELLED},
}, DelegationStatus)
