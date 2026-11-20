"""CommandCenter: wires the services together and implements output ingestion.

Framework-neutral composition root. A web backend constructs one per organization (with
PostgreSQL-backed repositories and audit store) and calls these methods from its handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .agents import DEFAULT_AGENT_DIR, AgentRegistry
from .approvals import Approval, ApprovalEngine
from .audit import AuditLogger, AuditStore, InMemoryAuditStore
from .common import Clock, Principal, utc_now
from .errors import CommandCenterError, PolicyViolationError
from .executors import CompositeExecutor, DataProvider, InternalToolExecutor, MockExternalExecutor, ToolExecutor
from .gateway import ActionGateway, ActionOutcome, ActionStatus
from .memory import Memory, MemoryService
from .orchestration import OrchestrationService
from .outputs import AgentOutput, OutputService, OutputValidity
from .policy import DEFAULT_POLICY_PATH, OrganizationPolicy
from .recommendations import Recommendation, RecommendationService
from .runs import RunService
from .sources import SourceCatalog
from .state_machines import RunStatus
from .tasks import TaskService
from .tools import DEFAULT_TOOL_REGISTRY, ToolRegistry


@dataclass(frozen=True)
class IngestionReport:
    output_id: str
    actions: tuple[ActionOutcome, ...] = ()
    tasks: tuple[ActionOutcome, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()
    memories: tuple[Memory, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def pending_approvals(self) -> list[str]:
        return [a.approval_id for a in self.actions + self.tasks
                if a.status is ActionStatus.PENDING_APPROVAL and a.approval_id]


@dataclass
class CommandCenter:
    registry: AgentRegistry
    policy: OrganizationPolicy
    tools: ToolRegistry
    audit: AuditLogger
    runs: RunService
    tasks: TaskService
    approvals: ApprovalEngine
    gateway: ActionGateway
    outputs: OutputService
    recommendations: RecommendationService
    memory: MemoryService
    orchestration: OrchestrationService
    external_executor: ToolExecutor
    internal_executor: InternalToolExecutor
    clock: Clock = field(default=utc_now)

    @classmethod
    def build(
        cls,
        *,
        agent_dir: str | Path = DEFAULT_AGENT_DIR,
        policy_path: str | Path = DEFAULT_POLICY_PATH,
        policy: OrganizationPolicy | None = None,
        registry: AgentRegistry | None = None,
        audit_store: AuditStore | None = None,
        external_executor: ToolExecutor | None = None,
        data_provider: DataProvider | None = None,
        clock: Clock = utc_now,
        tools: ToolRegistry = DEFAULT_TOOL_REGISTRY,
    ) -> "CommandCenter":
        registry = registry or AgentRegistry.load_dir(agent_dir, tools)
        policy = policy or OrganizationPolicy.load(policy_path, tools)
        audit = AuditLogger(audit_store or InMemoryAuditStore(), clock)
        runs = RunService(registry, audit, clock=clock)
        tasks = TaskService(audit, clock=clock)
        approvals = ApprovalEngine(policy, audit, clock=clock)
        internal = InternalToolExecutor(tasks, audit, clock)
        external = external_executor or MockExternalExecutor()
        executor = CompositeExecutor(tools, internal, external, data_provider)
        gateway = ActionGateway(registry, tools, policy, runs, approvals, executor, audit)
        return cls(registry=registry, policy=policy, tools=tools, audit=audit, runs=runs, tasks=tasks,
                   approvals=approvals, gateway=gateway, outputs=OutputService(registry, audit, clock=clock),
                   recommendations=RecommendationService(audit, clock=clock),
                   memory=MemoryService(audit, clock=clock),
                   orchestration=OrchestrationService(registry, runs, audit, policy.orchestration, clock=clock),
                   external_executor=external, internal_executor=internal, clock=clock)

    # -- output ingestion --------------------------------------------------------------
    def ingest_output(self, output_id: str) -> IngestionReport:
        """Turn a VALID structured output into governed records.

        tasks              -> create_task through the gateway (same permission + risk path)
        recommendations    -> RecommendationService (PROPOSED; humans decide)
        proposed_actions   -> gateway.submit (risk-classified; approvals created by policy)
        memory_candidates  -> MemoryService.propose (PROPOSED; never auto-accepted)
        approval_requests in the response are advisory; real approvals come from policy.
        """
        out: AgentOutput = self.outputs.get(output_id)
        if out.validity is not OutputValidity.VALID:
            raise PolicyViolationError("invalid outputs cannot be ingested: " + "; ".join(out.validation_errors))
        run = self.runs.get(out.run_id)
        if run.status is not RunStatus.RUNNING:
            raise PolicyViolationError(f"run is {run.status.value}; outputs are ingested while RUNNING")
        agent = self.registry.get_agent(out.agent_id)
        self.outputs.mark_ingested(output_id)
        content = out.content
        sources = SourceCatalog.from_response(content)
        errors: list[str] = []

        task_outcomes = []
        for t in content["tasks"]:
            task_outcomes.append(self.gateway.submit(run.run_id, {
                "tool": "create_task", "action_type": "create_task", "parameters": t,
                "risk_class": t.get("risk_level"), "reason": f"task proposed in output {output_id}"},
                pause_run=False))

        recs = []
        for r in content["recommendations"]:
            try:
                recs.append(self.recommendations.create(r, organization_id=run.organization_id,
                                                        source_agent=agent.agent_id, run_id=run.run_id,
                                                        sources=sources))
            except CommandCenterError as exc:
                errors.append(f"recommendation rejected: {exc}")

        action_outcomes = [self.gateway.submit(run.run_id, a, pause_run=False) for a in content["proposed_actions"]]

        memories = []
        for m in content["memory_candidates"]:
            try:
                memories.append(self.memory.propose(m, agent=agent, organization_id=run.organization_id,
                                                    run_id=run.run_id, sources=sources))
            except CommandCenterError as exc:
                errors.append(f"memory candidate rejected: {exc}")

        if self.approvals.pending_for_run(run.run_id) and self.runs.get(run.run_id).status is RunStatus.RUNNING:
            self.runs.transition(run.run_id, RunStatus.APPROVAL_REQUIRED, reason="output created approval requests")
        return IngestionReport(output_id, tuple(action_outcomes), tuple(task_outcomes), tuple(recs),
                               tuple(memories), tuple(errors))

    # -- approval decisions (resume the run when nothing is pending) -------------------
    def approve(self, approval_id: str, approver: Principal, note: str | None = None) -> Approval:
        return self.approvals.approve(approval_id, approver, note)

    def reject(self, approval_id: str, approver: Principal, reason: str) -> Approval:
        a = self.approvals.reject(approval_id, approver, reason)
        self.gateway.resume_if_unblocked(a.run_id)
        return a

    def execute_approved(self, approval_id: str, *, tool_name: str, action_type: str,
                         payload: Mapping[str, Any]) -> ActionOutcome:
        return self.gateway.execute_approved(approval_id, tool_name=tool_name, action_type=action_type,
                                             payload=payload)
