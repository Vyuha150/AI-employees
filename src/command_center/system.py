"""CommandCenter: wires the services together and implements output ingestion.

Framework-neutral composition root. A web backend constructs one per organization (with
PostgreSQL-backed repositories and audit store) and calls these methods from its handlers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .agents import DEFAULT_AGENT_DIR, AgentRegistry
from .alerts import AlertService
from .escalations import EscalationEngine
from .idempotency import InMemoryIdempotencyStore
from .knowledge import KeywordRetriever, KnowledgeService
from .kpis import KpiService
from .read_tools import KnowledgeReadConnector, KpiReadConnector, ReadToolService, TaskReadConnector
from .usage import UsageService
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
    alerts: AlertService | None = None
    kpis: KpiService | None = None
    knowledge: KnowledgeService | None = None
    usage: UsageService | None = None
    escalations: EscalationEngine | None = None
    idempotency: Any = None
    read_tools: ReadToolService | None = None
    runtime: Any = None
    models: Any = None
    sources_repo: Any = None
    repositories: Any = None
    conversations: Any = None

    def workflow_engine(self) -> Any:
        from .workflows import WorkflowEngine
        r = self.repositories
        kw = {}
        if r is not None and getattr(r, "workflows", None) is not None:
            kw = {"definitions": r.workflows, "runs": r.workflow_runs}
        return WorkflowEngine(self, self.runtime, **kw)

    def scheduler(self, dispatcher: Any, **kw: Any) -> Any:
        from .scheduler import Scheduler
        r = self.repositories
        if r is not None and getattr(r, "scheduled_jobs", None) is not None:
            kw.setdefault("jobs", r.scheduled_jobs)
            kw.setdefault("runs", r.scheduled_runs)
        return Scheduler(self.policy, self.registry, self.audit, dispatcher, idempotency=self.idempotency,
                         clock=self.clock, **kw)

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
        sleep: Callable[[float], None] = time.sleep,
        models: Any = None,
        providers: Any = None,
        read_connectors: Mapping[str, Any] | None = None,
        enforce_budgets: bool = True,
        repositories: Any = None,
    ) -> "CommandCenter":
        """repositories: optional object with attributes named like the services' repos
        (runs, tasks, approvals, outputs, recommendations, changes, memories, delegations, alerts,
        notifications, kpis, usage) to back the services with e.g. PostgreSQL."""
        policy = policy or OrganizationPolicy.load(policy_path, tools)
        if registry is None:
            registry = AgentRegistry.load_dir(agent_dir, tools)
        if policy.privacy_gate_open("read_people_summary") and not tools.exists("read_people_summary"):
            tools = tools.with_proposed("read_people_summary")
        repo = (lambda name: getattr(repositories, name, None)) if repositories is not None else (lambda name: None)

        def opt(**kw):  # pass a repository only when provided
            return {k: v for k, v in kw.items() if v is not None}

        audit = AuditLogger(audit_store or repo("audit") or InMemoryAuditStore(), clock)
        runs = RunService(registry, audit, clock=clock, **opt(repo=repo("runs")))
        tasks = TaskService(audit, clock=clock, **opt(repo=repo("tasks")))
        approvals = ApprovalEngine(policy, audit, clock=clock, **opt(repo=repo("approvals")))
        alerts = AlertService(audit, clock=clock, **opt(repo=repo("alerts"), notifications=repo("notifications")))
        kpis = KpiService(audit, clock=clock, **opt(repo=repo("kpis")))
        knowledge = KnowledgeService(policy.organization_id, audit, clock=clock,
                                     **opt(documents=repo("knowledge_documents"), chunks=repo("knowledge_chunks")))
        usage = UsageService(policy, audit, clock=clock, **opt(repo=repo("usage")))
        if enforce_budgets:
            runs.budget_guard = usage.check_can_start
        internal = InternalToolExecutor(tasks, audit, clock, alerts=alerts, kpis=kpis)
        external = external_executor or MockExternalExecutor()
        outputs = OutputService(registry, audit, clock=clock, evaluation_repo=repo("evaluations"),
                                **opt(repo=repo("outputs")))
        connectors: dict[str, Any] = {
            "read_kpis": KpiReadConnector(kpis, clock=clock),
            "read_tasks": TaskReadConnector(tasks, clock=clock),
        }
        retriever = KeywordRetriever(knowledge)
        scopes = lambda aid: registry.get_profile(aid).knowledge_scopes
        connectors["read_knowledge"] = KnowledgeReadConnector(retriever, scopes, clock=clock)
        connectors["search_company_data"] = KnowledgeReadConnector(retriever, scopes, clock=clock)
        if read_connectors is None and repositories is not None and getattr(repositories, "db", None) is not None:
            from .pg.read_connectors import pg_read_connectors   # PostgreSQL deployments only
            read_connectors = pg_read_connectors(repositories.db, repositories.org_uuid, scopes, clock)
            read_connectors.pop("read_knowledge", None)          # domain knowledge service already reads PG
            read_connectors.pop("search_company_data", None)
        connectors.update(read_connectors or {})
        read_tools = ReadToolService(connectors)
        executor = CompositeExecutor(tools, internal, external, data_provider, read_tools)
        gateway = ActionGateway(registry, tools, policy, runs, approvals, executor, audit, sleep=sleep)
        memory = MemoryService(audit, clock=clock, **opt(repo=repo("memories")))
        idem = repo("idempotency") or InMemoryIdempotencyStore(clock)
        if repo("agents") is not None:
            repo("agents").sync(registry)
        escalations = EscalationEngine(policy, audit, alerts, tasks, approvals, kpis, idem, clock)
        gateway.on_execution_unknown = escalations.on_execution_unknown
        cc = cls(registry=registry, policy=policy, tools=tools, audit=audit, runs=runs, tasks=tasks,
                 approvals=approvals, gateway=gateway, outputs=outputs,
                 recommendations=RecommendationService(audit, clock=clock, **opt(repo=repo("recommendations"),
                                                                                  changes=repo("changes"))),
                 memory=memory,
                 orchestration=OrchestrationService(registry, runs, audit, policy.orchestration, clock=clock,
                                                    **opt(repo=repo("delegations"))),
                 external_executor=external, internal_executor=internal, clock=clock, alerts=alerts, kpis=kpis,
                 knowledge=knowledge, usage=usage, escalations=escalations, idempotency=idem, read_tools=read_tools,
                 models=models, sources_repo=repo("sources"), repositories=repositories)
        from .conversations import ConversationService
        cc.conversations = ConversationService(policy.organization_id, audit, clock=clock,
                                               **opt(conversations=repo("conversations"), messages=repo("messages")))
        if models is not None and providers is not None:
            from .context import ContextAssembler
            from .runtime import AgentRuntime
            assembler = ContextAssembler(policy, audit, memory=memory, retriever=retriever, kpis=kpis,
                                         outputs=outputs, clock=clock)
            cc.runtime = AgentRuntime(cc, models, providers, assembler, usage, sleep=sleep)
        return cc

    @classmethod
    def from_config(cls, config: Any, *, providers: Any = None, **kw: Any) -> "CommandCenter":
        """Build from a validated config.AppConfig (the production entry point)."""
        return cls.build(policy=config.policy, registry=config.registry, tools=config.tools, models=config.models,
                         providers=providers, **kw)

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
        if out.kind.value != "STRUCTURED_RESPONSE":
            raise PolicyViolationError(f"{out.kind.value} outputs are reports and are not ingested")
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
