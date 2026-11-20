"""Approval workflow engine (Parts 6 and 7).

Guarantees
1. An approval is bound to one exact action: organization + agent + run + tool + action type
   + canonical payload (see canonical.action_binding_hash). Any change to any of them,
   including a single payload field, fails `can_execute`.
2. An approval authorizes at most one execution attempt (claim -> EXECUTED | FAILED).
   A failed execution needs a new approval.
3. Expired, rejected and cancelled approvals can never execute.
4. Every approval has an expiry. RED expiry is capped by organization policy, so a RED
   approval can never be blanket or permanent.
5. Only a HUMAN principal of the same organization holding the required role may approve or
   reject. The requesting agent can never approve its own request.
6. Approval is not execution. Execution results are recorded separately (execution_status,
   approval.executed / approval.execution_failed events, plus tool.executed / tool.failed).

History: every transition is an append-only audit event (object_type="approval"); the
record in the repository is the latest snapshot only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Mapping

from .audit import AuditEvent, AuditLogger, EventType
from .canonical import action_binding_hash
from .common import ActorType, Clock, Principal, RiskClass, StrEnum, new_id, utc_now
from .errors import (
    ApprovalError,
    ApprovalNotExecutableError,
    ConfigurationError,
    PayloadMismatchError,
    PermissionDeniedError,
)
from .policy import OrganizationPolicy
from .state_machines import APPROVAL_MACHINE, ApprovalStatus
from .storage import InMemoryRepository, Repository, frozen_copy


class ExecutionStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Approval:
    approval_id: str
    organization_id: str
    run_id: str
    agent_id: str
    tool_name: str
    action_type: str
    payload_hash: str
    payload: Mapping[str, Any]
    risk_class: RiskClass
    reason: str
    financial_impact: Mapping[str, Any] | None
    reversible: bool
    requested_at: datetime
    requested_by: str
    required_role: str
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_by: str | None = None
    rejected_at: datetime | None = None
    decision_note: str | None = None
    execution_status: ExecutionStatus = ExecutionStatus.NOT_STARTED
    execution_result_ref: str | None = None
    execution_error: str | None = None
    executed_at: datetime | None = None
    version: int = 0


@dataclass(frozen=True)
class ExecutionCheck:
    allowed: bool
    reason: str


@dataclass
class ApprovalEngine:
    policy: OrganizationPolicy
    audit: AuditLogger
    repo: Repository[Approval] = field(default_factory=lambda: InMemoryRepository("approval_id"))
    clock: Clock = utc_now
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- helpers -----------------------------------------------------------------------
    def _event(self, event_type: EventType, a: Approval, actor_type: ActorType, actor_id: str,
               prev: ApprovalStatus | None, payload: Mapping[str, Any] | None = None) -> None:
        self.audit.record(event_type, organization_id=a.organization_id, actor_type=actor_type, actor_id=actor_id,
                          agent_id=a.agent_id, run_id=a.run_id, object_type="approval", object_id=a.approval_id,
                          payload=payload, previous_state=prev, new_state=a.status)

    def _transition(self, a: Approval, target: ApprovalStatus, **changes: Any) -> Approval:
        APPROVAL_MACHINE.check(a.status, target)
        return self.repo.update(replace(a, status=target, **changes), expected_version=a.version)

    def get(self, approval_id: str) -> Approval:
        return self.repo.get(approval_id)

    def history(self, approval_id: str) -> list[AuditEvent]:
        return self.audit.store.events(object_type="approval", object_id=approval_id)

    # -- request -----------------------------------------------------------------------
    def request_approval(
        self,
        *,
        organization_id: str,
        run_id: str,
        agent_id: str,
        tool_name: str,
        action_type: str,
        payload: Mapping[str, Any],
        risk_class: RiskClass,
        reason: str,
        reversible: bool,
        requested_by: str,
        financial_impact: Mapping[str, Any] | None = None,
        required_role: str | None = None,
        ttl: timedelta | None = None,
    ) -> Approval:
        if organization_id != self.policy.organization_id:
            raise PermissionDeniedError("approval requested for a different organization")
        if not reason.strip():
            raise ApprovalError("an approval request must state a reason")
        now = self.clock()
        max_ttl = timedelta(hours=self.policy.max_ttl_hours[risk_class])
        ttl = ttl if ttl is not None else timedelta(hours=self.policy.approval_ttl_hours(risk_class))
        if ttl <= timedelta(0) or ttl > max_ttl:
            raise ConfigurationError(f"approval TTL must be positive and at most {max_ttl} for {risk_class.value}")
        policy_role = self.policy.approver_role(tool_name, risk_class)
        if required_role is not None and required_role != policy_role:
            raise PermissionDeniedError(
                f"required_role {required_role!r} differs from policy role {policy_role!r}; callers cannot choose approvers")

        payload_copy = frozen_copy(dict(payload))
        approval = Approval(
            approval_id=new_id("apr"),
            organization_id=organization_id,
            run_id=run_id,
            agent_id=agent_id,
            tool_name=tool_name,
            action_type=action_type,
            payload_hash=action_binding_hash(organization_id=organization_id, agent_id=agent_id, run_id=run_id,
                                             tool_name=tool_name, action_type=action_type, payload=payload_copy),
            payload=payload_copy,
            risk_class=risk_class,
            reason=reason,
            financial_impact=frozen_copy(dict(financial_impact)) if financial_impact else None,
            reversible=reversible,
            requested_at=now,
            requested_by=requested_by,
            required_role=policy_role,
            expires_at=now + ttl,
        )
        self.repo.add(approval)
        self._event(EventType.APPROVAL_REQUESTED, approval, ActorType.AGENT, requested_by, None, {
            "tool_name": tool_name, "action_type": action_type, "payload_hash": approval.payload_hash,
            "risk_class": risk_class.value, "required_role": policy_role,
            "expires_at": approval.expires_at.isoformat(), "reason": reason,
        })
        return approval

    # -- decisions ---------------------------------------------------------------------
    def _check_decider(self, a: Approval, principal: Principal) -> None:
        if not principal.is_human:
            raise PermissionDeniedError("only a human principal can decide an approval")
        if principal.organization_id != a.organization_id:
            raise PermissionDeniedError("approver belongs to a different organization")
        if principal.principal_id in (a.requested_by, a.agent_id):
            raise PermissionDeniedError("the requester cannot decide its own approval")
        if a.required_role not in principal.roles:
            raise PermissionDeniedError(f"approver lacks required role {a.required_role!r}")

    def _expire_if_due(self, a: Approval, now: datetime) -> Approval:
        if a.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED) and now >= a.expires_at \
                and a.execution_status is ExecutionStatus.NOT_STARTED:
            prev = a.status
            a = self._transition(a, ApprovalStatus.EXPIRED)
            self._event(EventType.APPROVAL_EXPIRED, a, ActorType.SYSTEM, "system", prev)
        return a

    def approve(self, approval_id: str, approver: Principal, note: str | None = None) -> Approval:
        with self._lock:
            a = self.repo.get(approval_id)
            self._check_decider(a, approver)
            now = self.clock()
            a = self._expire_if_due(a, now)
            if a.status is not ApprovalStatus.PENDING:
                raise ApprovalError(f"approval {approval_id} is {a.status.value}, not PENDING")
            a = self._transition(a, ApprovalStatus.APPROVED, approved_by=approver.principal_id, approved_at=now,
                                 decision_note=note)
            self._event(EventType.APPROVAL_APPROVED, a, ActorType.HUMAN, approver.principal_id,
                        ApprovalStatus.PENDING, {"payload_hash": a.payload_hash, "note": note})
            return a

    def reject(self, approval_id: str, approver: Principal, reason: str) -> Approval:
        if not reason.strip():
            raise ApprovalError("a rejection must state a reason")
        with self._lock:
            a = self.repo.get(approval_id)
            self._check_decider(a, approver)
            now = self.clock()
            a = self._expire_if_due(a, now)
            if a.status is not ApprovalStatus.PENDING:
                raise ApprovalError(f"approval {approval_id} is {a.status.value}, not PENDING")
            a = self._transition(a, ApprovalStatus.REJECTED, rejected_by=approver.principal_id, rejected_at=now,
                                 decision_note=reason)
            self._event(EventType.APPROVAL_REJECTED, a, ActorType.HUMAN, approver.principal_id,
                        ApprovalStatus.PENDING, {"reason": reason})
            return a

    def cancel(self, approval_id: str, actor_type: ActorType, actor_id: str, reason: str) -> Approval:
        with self._lock:
            a = self.repo.get(approval_id)
            if a.execution_status is not ExecutionStatus.NOT_STARTED:
                raise ApprovalError("cannot cancel an approval whose execution has started")
            prev = a.status
            a = self._transition(a, ApprovalStatus.CANCELLED, decision_note=reason)
            self._event(EventType.APPROVAL_CANCELLED, a, actor_type, actor_id, prev, {"reason": reason})
            return a

    def expire(self, approval_id: str) -> Approval:
        """Force-expire (e.g. by an administrator or a superseding request)."""
        with self._lock:
            a = self.repo.get(approval_id)
            if a.execution_status is not ExecutionStatus.NOT_STARTED:
                raise ApprovalError("cannot expire an approval whose execution has started")
            prev = a.status
            a = self._transition(a, ApprovalStatus.EXPIRED)
            self._event(EventType.APPROVAL_EXPIRED, a, ActorType.SYSTEM, "system", prev)
            return a

    def expire_due(self) -> list[Approval]:
        now = self.clock()
        with self._lock:
            return [self._expire_if_due(a, now) for a in self.repo.list(
                lambda a: a.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED) and now >= a.expires_at
                and a.execution_status is ExecutionStatus.NOT_STARTED)]

    # -- execution gate ----------------------------------------------------------------
    def can_execute(
        self,
        approval_id: str,
        *,
        organization_id: str,
        agent_id: str,
        run_id: str,
        tool_name: str,
        action_type: str,
        payload: Mapping[str, Any],
    ) -> ExecutionCheck:
        a = self.repo.find(approval_id)
        if a is None:
            return ExecutionCheck(False, "approval not found")
        if a.status is not ApprovalStatus.APPROVED:
            return ExecutionCheck(False, f"approval status is {a.status.value}")
        if a.execution_status is not ExecutionStatus.NOT_STARTED:
            return ExecutionCheck(False, "approval has already been used")
        if self.clock() >= a.expires_at:
            return ExecutionCheck(False, "approval has expired")
        try:
            presented = action_binding_hash(organization_id=organization_id, agent_id=agent_id, run_id=run_id,
                                            tool_name=tool_name, action_type=action_type, payload=payload)
        except ValueError as exc:
            return ExecutionCheck(False, f"payload cannot be canonicalized: {exc}")
        if presented != a.payload_hash:
            return ExecutionCheck(False, "action differs from the approved action (payload or binding changed)")
        return ExecutionCheck(True, "approved action matches")

    def begin_execution(self, approval_id: str, *, organization_id: str, agent_id: str, run_id: str,
                        tool_name: str, action_type: str, payload: Mapping[str, Any]) -> Approval:
        """Atomically claim the approval for one execution attempt."""
        with self._lock:
            a = self.repo.get(approval_id)
            a = self._expire_if_due(a, self.clock())
            check = self.can_execute(approval_id, organization_id=organization_id, agent_id=agent_id, run_id=run_id,
                                     tool_name=tool_name, action_type=action_type, payload=payload)
            if not check.allowed:
                if "differs" in check.reason:
                    raise PayloadMismatchError(check.reason)
                raise ApprovalNotExecutableError(check.reason)
            a = self.repo.update(replace(a, execution_status=ExecutionStatus.IN_PROGRESS), expected_version=a.version)
            self._event(EventType.APPROVAL_EXECUTION_STARTED, a, ActorType.SYSTEM, "system", a.status)
            return a

    def mark_executed(self, approval_id: str, result_ref: str | None = None) -> Approval:
        with self._lock:
            a = self.repo.get(approval_id)
            if a.execution_status is not ExecutionStatus.IN_PROGRESS:
                raise ApprovalError("mark_executed requires an execution in progress")
            a = self._transition(a, ApprovalStatus.EXECUTED, execution_status=ExecutionStatus.SUCCEEDED,
                                 execution_result_ref=result_ref, executed_at=self.clock())
            self._event(EventType.APPROVAL_EXECUTED, a, ActorType.SYSTEM, "system", ApprovalStatus.APPROVED,
                        {"result_ref": result_ref})
            return a

    def mark_failed(self, approval_id: str, error: str) -> Approval:
        with self._lock:
            a = self.repo.get(approval_id)
            if a.execution_status is not ExecutionStatus.IN_PROGRESS:
                raise ApprovalError("mark_failed requires an execution in progress")
            a = self._transition(a, ApprovalStatus.FAILED, execution_status=ExecutionStatus.FAILED,
                                 execution_error=error, executed_at=self.clock())
            self._event(EventType.APPROVAL_EXECUTION_FAILED, a, ActorType.SYSTEM, "system", ApprovalStatus.APPROVED,
                        {"error": error})
            return a

    def mark_unknown(self, approval_id: str, detail: str) -> Approval:
        """The executor could not tell whether the external effect happened (e.g. timeout).
        The approval is NOT reusable and the action is never retried automatically."""
        with self._lock:
            a = self.repo.get(approval_id)
            if a.execution_status is not ExecutionStatus.IN_PROGRESS:
                raise ApprovalError("mark_unknown requires an execution in progress")
            a = self._transition(a, ApprovalStatus.EXECUTION_UNKNOWN, execution_status=ExecutionStatus.UNKNOWN,
                                 execution_error=detail, executed_at=self.clock())
            self._event(EventType.APPROVAL_EXECUTION_UNKNOWN, a, ActorType.SYSTEM, "system", ApprovalStatus.APPROVED,
                        {"detail": detail, "risk_class": a.risk_class.value})
            return a

    def reconcile(self, approval_id: str, principal: Principal, *, executed: bool, evidence_ref: str,
                  note: str) -> Approval:
        """A human with the approval's required role records what actually happened, with evidence."""
        if not evidence_ref.strip() or not note.strip():
            raise ApprovalError("reconciliation needs an evidence reference and a note")
        with self._lock:
            a = self.repo.get(approval_id)
            if a.status is not ApprovalStatus.EXECUTION_UNKNOWN:
                raise ApprovalError("only EXECUTION_UNKNOWN approvals can be reconciled")
            if not principal.is_human or principal.organization_id != a.organization_id \
                    or a.required_role not in principal.roles:
                raise PermissionDeniedError("reconciliation requires a human with the approval's required role")
            target = ApprovalStatus.EXECUTED if executed else ApprovalStatus.FAILED
            a = self._transition(a, target,
                                 execution_status=ExecutionStatus.SUCCEEDED if executed else ExecutionStatus.FAILED,
                                 execution_result_ref=evidence_ref if executed else a.execution_result_ref,
                                 decision_note=note)
            self._event(EventType.APPROVAL_RECONCILED, a, ActorType.HUMAN, principal.principal_id,
                        ApprovalStatus.EXECUTION_UNKNOWN, {"executed": executed, "evidence_ref": evidence_ref,
                                                           "note": note})
            return a

    def pending_for_run(self, run_id: str) -> list[Approval]:
        return self.repo.list(lambda a: a.run_id == run_id and a.status is ApprovalStatus.PENDING)
