"""Action gateway: the single path from an agent's proposed action to execution.

  submit():
    run RUNNING? -> agent valid & enabled? -> tool known? -> tool allowed for this agent
    (allowed_tools, role, not prohibited)? -> tool.requested -> application risk
    classification (agent declaration advisory; discrepancies audited) -> approval decision
      no approval needed -> execute -> tool.executed | tool.failed
      approval needed    -> approval.requested, run -> APPROVAL_REQUIRED
  execute_approved():
    re-checks agent and tool permissions at execution time, verifies the presented action
    against the approval binding (payload hash), claims the approval (single use), executes,
    and records the result separately from the approval.

Nothing here reports success unless the executor returned success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from .agents import AgentProfile, AgentRegistry
from .approvals import Approval, ApprovalEngine
from .audit import AuditLogger, EventType
from .common import ActorType, RiskClass, StrEnum, new_id
from .errors import CommandCenterError, UnknownToolError
from .executors import ToolExecutor, ToolInvocation, ToolResult, ToolStatus
from .retries import RetryClass, run_with_retries, write_retry_allowed
from .policy import OrganizationPolicy
from .risk import RiskAssessment, RiskContext, classify_action, requires_approval
from .runs import RunService
from .state_machines import ApprovalStatus, RunStatus
from .tools import PROPOSED_TOOLS, ToolCategory, ToolRegistry


class ActionStatus(StrEnum):
    DENIED = "DENIED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"


@dataclass(frozen=True)
class ActionOutcome:
    status: ActionStatus
    tool_name: str
    reason: str
    assessment: RiskAssessment | None = None
    approval_id: str | None = None
    result: ToolResult | None = None


_LIVE_APPROVAL = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED, ApprovalStatus.EXECUTED)


@dataclass
class ActionGateway:
    registry: AgentRegistry
    tools: ToolRegistry
    policy: OrganizationPolicy
    runs: RunService
    approvals: ApprovalEngine
    executor: ToolExecutor
    audit: AuditLogger
    aggregation_window: timedelta = field(default=timedelta(hours=24))
    sleep: Callable[[float], None] = time.sleep
    on_execution_unknown: Callable[[Approval, ToolResult], None] | None = None

    # ---------------------------------------------------------------------------------
    def _deny(self, run, tool_name: str, reason: str) -> ActionOutcome:
        self.audit.record(EventType.TOOL_DENIED, organization_id=run.organization_id, actor_type=ActorType.SYSTEM,
                          actor_id="gateway", agent_id=run.agent_id, run_id=run.run_id, object_type="tool",
                          object_id=tool_name, payload={"reason": reason})
        return ActionOutcome(ActionStatus.DENIED, tool_name, reason)

    def _permission_problem(self, agent: AgentProfile, tool_name: str) -> str | None:
        if tool_name in PROPOSED_TOOLS and not self.tools.exists(tool_name):
            return (f"tool {tool_name!r} is privacy-gated and not enabled; data unavailable "
                    "until a privacy review approves it")
        if not self.tools.exists(tool_name):
            return f"unknown tool {tool_name!r}"
        if tool_name in PROPOSED_TOOLS and not self.policy.privacy_gate_open(tool_name):
            return f"tool {tool_name!r} privacy gate is not approved; data unavailable"
        if tool_name in agent.prohibited_actions:
            return f"tool {tool_name!r} is prohibited for {agent.agent_id}"
        if tool_name not in agent.allowed_tools:
            return f"tool {tool_name!r} is not in {agent.agent_id}'s allowed_tools"
        if not self.tools.role_may_use(agent.role, tool_name):
            return f"role {agent.role.value} may not use {tool_name!r}"
        return None

    def _prior_amount(self, agent_id: str, tool_name: str) -> Decimal:
        rule = self.policy.monetary.get(tool_name) or self.policy.global_monetary
        if rule is None:
            return Decimal(0)
        since = self.approvals.clock() - self.aggregation_window
        total = Decimal(0)
        for a in self.approvals.repo.list(lambda a: a.agent_id == agent_id and a.tool_name == tool_name
                                          and a.status in _LIVE_APPROVAL and a.requested_at >= since):
            try:
                total += Decimal(str(a.payload.get(rule.amount_field, 0)))
            except (InvalidOperation, ValueError):
                continue
        return total

    def _record_result(self, inv: ToolInvocation, result: ToolResult, risk: RiskClass) -> None:
        if result.status is ToolStatus.EXECUTION_UNKNOWN:
            event = EventType.TOOL_EXECUTION_UNKNOWN
        else:
            event = EventType.TOOL_EXECUTED if result.success else EventType.TOOL_FAILED
        self.audit.record(event,
                          organization_id=inv.organization_id, actor_type=ActorType.AGENT, actor_id=inv.agent_id,
                          agent_id=inv.agent_id, run_id=inv.run_id, object_type="tool", object_id=inv.tool_name,
                          payload={"action_type": inv.action_type, "risk_class": risk.value,
                                   "approval_id": inv.approval_id, "result_ref": result.result_ref,
                                   "tool_call_id": inv.tool_call_id, "execution_id": inv.execution_id,
                                   "status": result.status.value, "error": result.error, "mock": result.mock,
                                   "source_ids": list(result.source_ids)})

    def _execute_once(self, inv: ToolInvocation, spec) -> ToolResult:
        try:
            result = self.executor.execute(inv)
        except Exception as exc:
            if spec.category in (ToolCategory.EXTERNAL, ToolCategory.HIGH_RISK):
                # The external system may or may not have acted: never call this a failure.
                result = ToolResult(False, error=f"{type(exc).__name__}: {exc}", status=ToolStatus.EXECUTION_UNKNOWN)
            else:  # internal/read tools are local; a crash means nothing was committed
                result = ToolResult(False, error=f"executor raised {type(exc).__name__}: {exc}")
        return replace(result, tool_call_id=result.tool_call_id or inv.tool_call_id,
                       tool_name=inv.tool_name, executed_at=result.executed_at or self.approvals.clock())

    def _run_executor(self, inv: ToolInvocation, risk: RiskClass = RiskClass.GREEN) -> ToolResult:
        spec = self.tools.get(inv.tool_name)
        if spec.category is ToolCategory.READ:
            setting, retry_class = self.policy.retries.read_tool, RetryClass.READ_TOOL_RETRY
            retryable = lambda r: r.status is ToolStatus.ERROR
        else:
            setting, retry_class = self.policy.retries.write_tool, RetryClass.WRITE_TOOL_RETRY
            retryable = lambda r: (r.status is ToolStatus.ERROR and write_retry_allowed(
                idempotent=spec.idempotent_with_execution_id, risk=risk, ambiguous=False).allowed)

        def attempt(n: int) -> ToolResult:
            if n > 1:
                self.audit.record(EventType.TOOL_RETRIED, organization_id=inv.organization_id,
                                  actor_type=ActorType.SYSTEM, actor_id="gateway", agent_id=inv.agent_id,
                                  run_id=inv.run_id, object_type="tool", object_id=inv.tool_name,
                                  payload={"attempt": n, "retry_class": retry_class.value,
                                           "execution_id": inv.execution_id})
            return self._execute_once(inv, spec)

        result, _ = run_with_retries(attempt, setting, retryable, sleep=self.sleep)
        return result

    # ---------------------------------------------------------------------------------
    def submit(self, run_id: str, action: Mapping[str, Any], *, pause_run: bool = True) -> ActionOutcome:
        """`action` uses the schema's proposed_action shape (§26).

        pause_run=False lets a caller submit every action of one output before pausing the
        run once (see CommandCenter.ingest_output); the caller must then pause it."""
        run = self.runs.get(run_id)
        tool_name = str(action.get("tool", ""))
        if run.status is not RunStatus.RUNNING:
            return self._deny(run, tool_name, f"run is {run.status.value}, not RUNNING")
        try:
            agent = self.registry.get_agent(run.agent_id)
        except CommandCenterError as exc:
            return self._deny(run, tool_name, f"agent cannot execute: {exc}")
        problem = self._permission_problem(agent, tool_name)
        if problem:
            return self._deny(run, tool_name, problem)

        payload = action.get("parameters")
        action_type = str(action.get("action_type") or tool_name)
        if not isinstance(payload, Mapping):
            return self._deny(run, tool_name, "parameters must be an object")
        declared = None
        if action.get("risk_class") is not None:
            try:
                declared = RiskClass(action["risk_class"])
            except ValueError:
                return self._deny(run, tool_name, f"invalid declared risk class {action['risk_class']!r}")

        self.audit.record(EventType.TOOL_REQUESTED, organization_id=run.organization_id, actor_type=ActorType.AGENT,
                          actor_id=agent.agent_id, agent_id=agent.agent_id, run_id=run_id, object_type="tool",
                          object_id=tool_name, payload={"action_type": action_type, "declared_risk_class":
                                                        declared.value if declared else None,
                                                        "reason": action.get("reason")})
        try:
            assessment = classify_action(tool_name, payload, RiskContext(
                self.policy, declared, self.tools, self._prior_amount(agent.agent_id, tool_name)))
        except UnknownToolError as exc:  # pragma: no cover - guarded above
            return self._deny(run, tool_name, str(exc))

        common = dict(organization_id=run.organization_id, actor_type=ActorType.SYSTEM, actor_id="risk_engine",
                      agent_id=agent.agent_id, run_id=run_id, object_type="tool", object_id=tool_name)
        if assessment.declared_lower_than_policy:
            self.audit.record(EventType.RISK_OVERRIDDEN_BY_POLICY, **common, payload={
                "declared": declared.value, "policy": assessment.policy_class.value,
                "reasons": list(assessment.reasons)}, previous_state=declared, new_state=assessment.final_class)
        elif assessment.declared_higher_than_policy:
            self.audit.record(EventType.RISK_DECLARED_HIGHER_THAN_POLICY, **common, payload={
                "declared": declared.value, "policy": assessment.policy_class.value})

        decision = requires_approval(assessment, agent, self.policy, run.principal)
        spec = self.tools.get(tool_name)
        if not decision.required:
            inv = ToolInvocation(run.organization_id, agent.agent_id, run_id, tool_name, action_type, dict(payload),
                                 tool_call_id=action.get("tool_call_id") or action.get("action_id"),
                                 execution_id=new_id("exe"), principal=run.principal)
            result = self._run_executor(inv, assessment.final_class)
            self._record_result(inv, result, assessment.final_class)
            return ActionOutcome(ActionStatus.EXECUTED if result.success else ActionStatus.FAILED, tool_name,
                                 decision.reason if result.success else (result.error or "execution failed"),
                                 assessment, result=result)

        approval = self.approvals.request_approval(
            organization_id=run.organization_id, run_id=run_id, agent_id=agent.agent_id, tool_name=tool_name,
            action_type=action_type, payload=payload, risk_class=assessment.final_class,
            reason=str(action.get("reason") or decision.reason), reversible=spec.reversible and bool(
                action.get("reversible", spec.reversible)),
            requested_by=agent.agent_id, financial_impact=action.get("financial_impact"))
        if pause_run and run.status is RunStatus.RUNNING:
            self.runs.transition(run_id, RunStatus.APPROVAL_REQUIRED, reason=f"awaiting {approval.approval_id}")
        return ActionOutcome(ActionStatus.PENDING_APPROVAL, tool_name, decision.reason, assessment,
                             approval_id=approval.approval_id)

    # ---------------------------------------------------------------------------------
    def execute_approved(self, approval_id: str, *, tool_name: str, action_type: str,
                         payload: Mapping[str, Any]) -> ActionOutcome:
        """Execute exactly the presented action, only if it matches the approval binding."""
        approval: Approval = self.approvals.get(approval_id)
        run = self.runs.get(approval.run_id)
        try:
            agent = self.registry.get_agent(approval.agent_id)
        except CommandCenterError as exc:
            return self._deny(run, tool_name, f"agent cannot execute: {exc}")
        problem = self._permission_problem(agent, tool_name)
        if problem:
            return self._deny(run, tool_name, problem)

        # raises PayloadMismatchError / ApprovalNotExecutableError
        self.approvals.begin_execution(approval_id, organization_id=run.organization_id, agent_id=agent.agent_id,
                                       run_id=run.run_id, tool_name=tool_name, action_type=action_type,
                                       payload=payload)
        # execution_id = approval_id: one approval is one logical execution, so an idempotent
        # adapter can de-duplicate a repeated delivery of the same call.
        inv = ToolInvocation(run.organization_id, agent.agent_id, run.run_id, tool_name, action_type, dict(payload),
                             approval_id, tool_call_id=approval_id, execution_id=approval_id,
                             principal=run.principal)
        result = self._run_executor(inv, approval.risk_class)
        self._record_result(inv, result, approval.risk_class)
        if result.status is ToolStatus.EXECUTION_UNKNOWN:
            updated = self.approvals.mark_unknown(approval_id, result.error or "execution outcome unknown")
            if self.on_execution_unknown is not None:
                self.on_execution_unknown(updated, result)
            self.resume_if_unblocked(run.run_id)
            return ActionOutcome(ActionStatus.EXECUTION_UNKNOWN, tool_name,
                                 "external outcome unknown; reconciliation required, no automatic retry",
                                 approval_id=approval_id, result=result)
        if result.success:
            self.approvals.mark_executed(approval_id, result.result_ref)
        else:
            self.approvals.mark_failed(approval_id, result.error or "execution failed")
        self.resume_if_unblocked(run.run_id)
        return ActionOutcome(ActionStatus.EXECUTED if result.success else ActionStatus.FAILED, tool_name,
                             "executed under approval" if result.success else (result.error or "execution failed"),
                             approval_id=approval_id, result=result)

    def resume_if_unblocked(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        if run.status is RunStatus.APPROVAL_REQUIRED and not self.approvals.pending_for_run(run_id):
            self.runs.transition(run_id, RunStatus.RUNNING, reason="no pending approvals")
