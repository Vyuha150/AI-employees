"""Agent runs (Part 9): creation, state transitions, usage and cost limits.

A run can only be created for a valid, enabled agent, by a principal holding
`run_agent:<agent_id>`. Delegated runs carry the parent's principal, who must ALSO hold
`run_agent:<target>`, so delegation never widens what a human could start directly.
Terminal runs never resume; a retry is a new run linked by `retry_of`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Mapping

from .agents import AgentProfile, AgentRegistry, RunContext, build_prompt_profile
from .audit import AuditLogger, EventType
from .common import ActorType, Clock, Principal, new_id, utc_now
from .errors import BudgetExceededError, InvalidTransitionError, PermissionDeniedError
from .prompt import build_system_prompt
from .state_machines import RUN_MACHINE, RunStatus
from .storage import InMemoryRepository, Repository


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    organization_id: str
    agent_id: str
    triggered_by: str
    principal: Principal
    objective: str
    created_at: datetime
    status: RunStatus = RunStatus.QUEUED
    parent_run_id: str | None = None
    delegation_id: str | None = None
    depth: int = 0
    retry_of: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    tokens_used: int = 0
    cost_used: float = 0.0
    failure_reason: str | None = None
    # model metadata (Part 53): what actually served the run, and any configured fallback
    model_provider: str | None = None
    model_name: str | None = None
    fallback_used: bool = False
    fallback_from: str | None = None
    cost_unknown: bool = False
    version: int = 0


_STATE_EVENTS = {
    RunStatus.RUNNING: EventType.AGENT_RUN_STARTED,
    RunStatus.SUCCEEDED: EventType.AGENT_RUN_COMPLETED,
    RunStatus.FAILED: EventType.AGENT_RUN_FAILED,
    RunStatus.CANCELLED: EventType.AGENT_RUN_CANCELLED,
}


@dataclass
class RunService:
    registry: AgentRegistry
    audit: AuditLogger
    repo: Repository[AgentRun] = field(default_factory=lambda: InMemoryRepository("run_id"))
    clock: Clock = utc_now
    # Called with the agent profile before a run is created; raises BudgetExceededError to block.
    budget_guard: Callable[[AgentProfile], Any] | None = None

    def get(self, run_id: str) -> AgentRun:
        return self.repo.get(run_id)

    def create_run(
        self,
        agent_id: str,
        principal: Principal,
        objective: str,
        *,
        parent_run_id: str | None = None,
        delegation_id: str | None = None,
        retry_of: str | None = None,
    ) -> AgentRun:
        agent = self.registry.get_agent(agent_id)  # fail closed: unknown/missing/invalid/disabled raise
        if not principal.is_human:
            raise PermissionDeniedError("runs are triggered on behalf of a human principal")
        if not principal.has(f"run_agent:{agent_id}"):
            raise PermissionDeniedError(f"principal {principal.principal_id!r} may not run agent {agent_id!r}")
        if not objective.strip():
            raise ValueError("a run needs an objective")

        depth = 0
        if parent_run_id is not None:
            parent = self.repo.get(parent_run_id)
            if parent.principal.principal_id != principal.principal_id:
                raise PermissionDeniedError("a delegated run must act for the parent run's principal")
            depth = parent.depth + 1
            if delegation_id is None:
                raise PermissionDeniedError("child runs can only be created through a delegation")
        if retry_of is not None:
            prior = self.repo.get(retry_of)
            if not RUN_MACHINE.is_terminal(prior.status) or prior.status is RunStatus.SUCCEEDED:
                raise InvalidTransitionError("only FAILED or CANCELLED runs can be retried")
            if prior.agent_id != agent_id:
                raise InvalidTransitionError("a retry must be for the same agent")

        if self.budget_guard is not None:
            self.budget_guard(agent)
        today = self.clock().date()
        runs_today = len(self.repo.list(lambda r: r.agent_id == agent_id and r.created_at.date() == today))
        if runs_today >= agent.cost_limits.max_runs_per_day:
            raise BudgetExceededError(f"agent {agent_id!r} reached max_runs_per_day ({agent.cost_limits.max_runs_per_day})")

        run = AgentRun(run_id=new_id("run"), organization_id=principal.organization_id, agent_id=agent_id,
                       triggered_by=principal.principal_id, principal=principal, objective=objective,
                       created_at=self.clock(), parent_run_id=parent_run_id, delegation_id=delegation_id,
                       depth=depth, retry_of=retry_of)
        self.repo.add(run)
        self.audit.record(EventType.AGENT_RUN_CREATED, organization_id=run.organization_id,
                          actor_type=ActorType.HUMAN, actor_id=principal.principal_id, agent_id=agent_id,
                          run_id=run.run_id, object_type="agent_run", object_id=run.run_id,
                          payload={"objective": objective, "parent_run_id": parent_run_id, "depth": depth,
                                   "delegation_id": delegation_id, "retry_of": retry_of},
                          new_state=run.status)
        return run

    def transition(self, run_id: str, target: RunStatus, *, reason: str | None = None,
                   actor_type: ActorType = ActorType.SYSTEM, actor_id: str = "system",
                   payload: Mapping[str, Any] | None = None) -> AgentRun:
        run = self.repo.get(run_id)
        RUN_MACHINE.check(run.status, target)
        now = self.clock()
        changes: dict[str, Any] = {"status": target}
        if target is RunStatus.RUNNING and run.started_at is None:
            changes["started_at"] = now
        if RUN_MACHINE.is_terminal(target):
            changes["ended_at"] = now
        if target is RunStatus.FAILED:
            changes["failure_reason"] = reason or "unspecified"
        updated = self.repo.update(replace(run, **changes), expected_version=run.version)
        event = _STATE_EVENTS.get(target, EventType.AGENT_RUN_STATE_CHANGED)
        if target is RunStatus.RUNNING and run.started_at is not None:
            event = EventType.AGENT_RUN_STATE_CHANGED  # resumed, not started
        self.audit.record(event, organization_id=run.organization_id, actor_type=actor_type, actor_id=actor_id,
                          agent_id=run.agent_id, run_id=run.run_id, object_type="agent_run", object_id=run.run_id,
                          payload={**({"reason": reason} if reason else {}), **(payload or {}),
                                   "tokens_used": updated.tokens_used, "cost_used": updated.cost_used},
                          previous_state=run.status, new_state=target)
        return updated

    def start(self, run_id: str) -> AgentRun:
        return self.transition(run_id, RunStatus.RUNNING)

    def record_usage(self, run_id: str, *, tokens: int, cost: float) -> AgentRun:
        """Add usage. Exceeding the agent's per-run limits fails the run and raises."""
        if tokens < 0 or cost < 0:
            raise ValueError("usage cannot be negative")
        run = self.repo.get(run_id)
        if RUN_MACHINE.is_terminal(run.status):
            raise InvalidTransitionError("cannot record usage on a finished run")
        agent = self.registry.get_profile(run.agent_id)
        updated = self.repo.update(replace(run, tokens_used=run.tokens_used + tokens, cost_used=run.cost_used + cost),
                                   expected_version=run.version)
        self.audit.record(EventType.AGENT_RUN_USAGE_RECORDED, organization_id=run.organization_id,
                          actor_type=ActorType.SYSTEM, actor_id="system", agent_id=run.agent_id, run_id=run.run_id,
                          object_type="agent_run", object_id=run.run_id,
                          payload={"tokens": tokens, "cost": cost, "currency": agent.cost_limits.currency,
                                   "tokens_used": updated.tokens_used, "cost_used": updated.cost_used})
        limits = agent.cost_limits
        if updated.tokens_used > limits.max_tokens_per_run or updated.cost_used > limits.max_cost_per_run:
            self.transition(run_id, RunStatus.FAILED, reason="budget_exceeded")
            raise BudgetExceededError(
                f"run {run_id} exceeded limits (tokens {updated.tokens_used}/{limits.max_tokens_per_run}, "
                f"cost {updated.cost_used:.4f}/{limits.max_cost_per_run} {limits.currency})")
        return updated

    def set_model(self, run_id: str, *, provider: str, model: str, fallback_from: str | None = None) -> AgentRun:
        run = self.repo.get(run_id)
        return self.repo.update(replace(run, model_provider=provider, model_name=model,
                                        fallback_used=run.fallback_used or fallback_from is not None,
                                        fallback_from=fallback_from or run.fallback_from),
                                expected_version=run.version)

    def mark_cost_unknown(self, run_id: str) -> AgentRun:
        run = self.repo.get(run_id)
        return self.repo.update(replace(run, cost_unknown=True), expected_version=run.version)

    def build_system_prompt(self, run_id: str, context: RunContext) -> str:
        run = self.repo.get(run_id)
        agent: AgentProfile = self.registry.get_agent(run.agent_id)
        if context.current_user.principal_id != run.principal.principal_id:
            raise PermissionDeniedError("run context user must be the run's principal")
        return build_system_prompt(build_prompt_profile(agent, context))
