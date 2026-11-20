"""Multi-agent orchestration contract (Part 18). Not an autonomous engine.

- Only an agent whose profile has delegation.can_delegate (the Chief of Staff) may delegate,
  and only to its configured allowed_targets.
- Delegation depth is bounded by policy (default 1): runs created by a delegation cannot
  delegate further, so recursion is impossible.
- Per-orchestration caps: distinct agents, total cost, total tokens, runtime.
- Each delegation reserves a budget; the sum of reservations may not exceed the caps.
- A delegation does not start anything by itself. The application starts the child run via
  `start_delegated_run`, which goes through RunService (principal permissions re-checked).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .agents import AgentRegistry
from .audit import AuditLogger, EventType
from .common import ActorType, Clock, StrEnum, new_id, utc_now
from .errors import BudgetExceededError, PermissionDeniedError, PolicyViolationError
from .policy import OrchestrationLimits
from .runs import AgentRun, RunService
from .state_machines import DELEGATION_MACHINE, RUN_MACHINE, DelegationStatus
from .storage import InMemoryRepository, Repository
from .tasks import Priority


class RequiredOutput(StrEnum):
    STRUCTURED_RESPONSE = "STRUCTURED_RESPONSE"
    ANALYSIS = "ANALYSIS"
    RECOMMENDATION = "RECOMMENDATION"
    REPORT = "REPORT"


@dataclass(frozen=True)
class DelegationBudget:
    max_cost: float
    max_tokens: int
    max_runtime_seconds: int

    def __post_init__(self) -> None:
        if self.max_cost <= 0 or self.max_tokens <= 0 or self.max_runtime_seconds <= 0:
            raise PolicyViolationError("delegation budgets must be positive")


@dataclass(frozen=True)
class Delegation:
    delegation_id: str
    organization_id: str
    parent_run_id: str
    requested_by_agent: str
    target_agent_id: str
    objective: str
    context_refs: tuple[str, ...]
    required_output: RequiredOutput
    priority: Priority
    budget: DelegationBudget
    created_at: datetime
    status: DelegationStatus = DelegationStatus.REQUESTED
    child_run_id: str | None = None
    version: int = 0


@dataclass
class OrchestrationService:
    registry: AgentRegistry
    runs: RunService
    audit: AuditLogger
    limits: OrchestrationLimits
    repo: Repository[Delegation] = field(default_factory=lambda: InMemoryRepository("delegation_id"))
    clock: Clock = utc_now

    def _root(self, run: AgentRun) -> AgentRun:
        while run.parent_run_id is not None:
            run = self.runs.get(run.parent_run_id)
        return run

    def for_orchestration(self, root_run_id: str) -> list[Delegation]:
        run_ids = {root_run_id}
        changed = True
        while changed:
            changed = False
            for d in self.repo.list():
                if d.parent_run_id in run_ids and d.child_run_id and d.child_run_id not in run_ids:
                    run_ids.add(d.child_run_id)
                    changed = True
        return self.repo.list(lambda d: d.parent_run_id in run_ids)

    def create_delegation(self, *, parent_run_id: str, target_agent_id: str, objective: str,
                          context_refs: list[str], required_output: RequiredOutput, priority: Priority,
                          budget: DelegationBudget) -> Delegation:
        parent = self.runs.get(parent_run_id)
        if RUN_MACHINE.is_terminal(parent.status):
            raise PolicyViolationError("cannot delegate from a finished run")
        requester = self.registry.get_agent(parent.agent_id)
        if not requester.delegation.can_delegate:
            raise PermissionDeniedError(f"agent {requester.agent_id!r} is not permitted to delegate")
        if target_agent_id not in requester.delegation.allowed_targets:
            raise PermissionDeniedError(f"{requester.agent_id!r} may not delegate to {target_agent_id!r}")
        target = self.registry.get_agent(target_agent_id)  # fail closed on unknown/disabled/invalid
        if parent.depth + 1 > self.limits.max_delegation_depth:
            raise PolicyViolationError(f"delegation depth {parent.depth + 1} exceeds max {self.limits.max_delegation_depth}")
        if not objective.strip():
            raise PolicyViolationError("a delegation needs an objective")

        root = self._root(parent)
        existing = [d for d in self.for_orchestration(root.run_id) if d.status is not DelegationStatus.CANCELLED]
        agents = {d.target_agent_id for d in existing} | {target_agent_id}
        if len(agents) > self.limits.max_agents_per_orchestration:
            raise PolicyViolationError(f"orchestration would involve {len(agents)} agents "
                                       f"(max {self.limits.max_agents_per_orchestration})")
        if budget.max_cost > target.cost_limits.max_cost_per_run or budget.max_tokens > target.cost_limits.max_tokens_per_run:
            raise BudgetExceededError("delegation budget exceeds the target agent's per-run limits")
        total_cost = sum(d.budget.max_cost for d in existing) + budget.max_cost + root.cost_used
        total_tokens = sum(d.budget.max_tokens for d in existing) + budget.max_tokens + root.tokens_used
        if total_cost > self.limits.max_total_cost:
            raise BudgetExceededError(f"orchestration cost budget {total_cost} exceeds {self.limits.max_total_cost}")
        if total_tokens > self.limits.max_total_tokens:
            raise BudgetExceededError(f"orchestration token budget {total_tokens} exceeds {self.limits.max_total_tokens}")
        elapsed = self.clock() - root.created_at
        if elapsed + timedelta(seconds=budget.max_runtime_seconds) > timedelta(seconds=self.limits.max_runtime_seconds):
            raise BudgetExceededError("delegation would exceed the orchestration runtime limit")

        d = Delegation(delegation_id=new_id("dlg"), organization_id=parent.organization_id,
                       parent_run_id=parent_run_id, requested_by_agent=requester.agent_id,
                       target_agent_id=target_agent_id, objective=objective, context_refs=tuple(context_refs),
                       required_output=required_output, priority=priority, budget=budget, created_at=self.clock())
        self.repo.add(d)
        self.audit.record(EventType.DELEGATION_CREATED, organization_id=d.organization_id, actor_type=ActorType.AGENT,
                          actor_id=requester.agent_id, agent_id=requester.agent_id, run_id=parent_run_id,
                          object_type="delegation", object_id=d.delegation_id,
                          payload={"target_agent_id": target_agent_id, "objective": objective,
                                   "budget": {"max_cost": budget.max_cost, "max_tokens": budget.max_tokens,
                                              "max_runtime_seconds": budget.max_runtime_seconds}},
                          new_state=d.status)
        return d

    def _transition(self, d: Delegation, target: DelegationStatus, **changes: Any) -> Delegation:
        DELEGATION_MACHINE.check(d.status, target)
        updated = self.repo.update(replace(d, status=target, **changes), expected_version=d.version)
        self.audit.record(EventType.DELEGATION_UPDATED, organization_id=d.organization_id,
                          actor_type=ActorType.SYSTEM, actor_id="system", agent_id=d.target_agent_id,
                          run_id=d.parent_run_id, object_type="delegation", object_id=d.delegation_id,
                          previous_state=d.status, new_state=target)
        return updated

    def start_delegated_run(self, delegation_id: str) -> AgentRun:
        d = self.repo.get(delegation_id)
        parent = self.runs.get(d.parent_run_id)
        child = self.runs.create_run(d.target_agent_id, parent.principal, d.objective,
                                     parent_run_id=parent.run_id, delegation_id=d.delegation_id)
        self._transition(d, DelegationStatus.RUNNING, child_run_id=child.run_id)
        return child

    def complete(self, delegation_id: str) -> Delegation:
        return self._transition(self.repo.get(delegation_id), DelegationStatus.COMPLETED)

    def fail(self, delegation_id: str) -> Delegation:
        return self._transition(self.repo.get(delegation_id), DelegationStatus.FAILED)

    def cancel(self, delegation_id: str) -> Delegation:
        return self._transition(self.repo.get(delegation_id), DelegationStatus.CANCELLED)
