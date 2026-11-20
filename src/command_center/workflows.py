"""Workflow engine foundation (Parts 49-50). Backend model + execution state; no editor.

Safety (Part 50)
- A workflow definition can never override tool risk or approvals. Keys that try
  (skip_approval, bypass_approval, approval_required, risk_class, auto_approve, force, ...)
  are rejected when the definition is validated.
- Even a valid definition gains nothing: TOOL and KPI_UPDATE steps execute through
  ActionGateway.submit exactly like an agent's proposed action, so a RED tool always creates
  a RED approval and the workflow waits for it.
- AGENT steps run through AgentRuntime (same gateway path). APPROVAL steps are human gates
  checked against a role; HUMAN_TASK waits for a task to be COMPLETED.
- Only human principals activate, pause or archive definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Mapping

from .agents import RunContext
from .audit import EventType
from .common import ActorType, Principal, StrEnum, new_id
from .errors import InvalidTransitionError, PermissionDeniedError, PolicyViolationError, ValidationFailedError
from .gateway import ActionStatus
from .state_machines import RUN_MACHINE, RunStatus, StateMachine, TaskStatus, _machine
from .storage import InMemoryRepository, Repository


class StepType(StrEnum):
    AGENT = "AGENT"
    TOOL = "TOOL"
    CONDITION = "CONDITION"
    APPROVAL = "APPROVAL"
    HUMAN_TASK = "HUMAN_TASK"
    NOTIFICATION = "NOTIFICATION"
    DELAY = "DELAY"
    KPI_UPDATE = "KPI_UPDATE"
    END = "END"


class WorkflowStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class WorkflowRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


W, WR = WorkflowStatus, WorkflowRunStatus
WORKFLOW_MACHINE: StateMachine = _machine("workflow", {
    W.DRAFT: {W.ACTIVE, W.ARCHIVED}, W.ACTIVE: {W.PAUSED, W.ARCHIVED}, W.PAUSED: {W.ACTIVE, W.ARCHIVED},
}, WorkflowStatus)
WORKFLOW_RUN_MACHINE: StateMachine = _machine("workflow_run", {
    WR.QUEUED: {WR.RUNNING, WR.CANCELLED},
    WR.RUNNING: {WR.WAITING, WR.SUCCEEDED, WR.FAILED, WR.CANCELLED},
    WR.WAITING: {WR.RUNNING, WR.FAILED, WR.CANCELLED},
}, WorkflowRunStatus)

FORBIDDEN_CONFIG_KEYS = frozenset({
    "skip_approval", "bypass_approval", "approval_required", "requires_approval", "auto_approve", "approved",
    "risk_class", "risk_override", "override_risk", "force", "no_approval", "skip_gateway", "approval_id",
})

_REQUIRED = {
    StepType.AGENT: {"agent_id", "objective"},
    StepType.TOOL: {"agent_id", "tool", "parameters"},
    StepType.CONDITION: {"ref", "equals", "on_true", "on_false"},
    StepType.APPROVAL: {"required_role", "summary"},
    StepType.HUMAN_TASK: {"title", "owner_role", "completion_criteria"},
    StepType.NOTIFICATION: {"recipient", "subject", "body"},
    StepType.DELAY: {"minutes"},
    StepType.KPI_UPDATE: {"agent_id", "kpi", "value", "unit", "observed_at", "source_reference"},
    StepType.END: set(),
}


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    type: StepType
    config: Mapping[str, Any] = field(default_factory=dict)
    next: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    organization_id: str
    name: str
    definition_version: int
    steps: tuple[WorkflowStep, ...]
    start_step: str
    created_by: str
    status: WorkflowStatus = WorkflowStatus.DRAFT
    version: int = 0                  # optimistic-concurrency counter

    def step(self, step_id: str) -> WorkflowStep:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        raise ValidationFailedError(f"unknown step {step_id!r}")


@dataclass(frozen=True)
class WorkflowRun:
    workflow_run_id: str
    organization_id: str
    workflow_id: str
    workflow_version: int
    principal: Principal
    status: WorkflowRunStatus
    current_step: str | None
    state: Mapping[str, Any]
    started_at: datetime
    waiting_on: Mapping[str, Any] | None = None
    ended_at: datetime | None = None
    error: str | None = None
    version: int = 0


def _walk_forbidden(node: Any, path: str, out: list[str]) -> None:
    if isinstance(node, Mapping):
        for k, v in node.items():
            if str(k).lower() in FORBIDDEN_CONFIG_KEYS:
                out.append(f"{path}.{k}: workflows cannot override approval or risk policy")
            _walk_forbidden(v, f"{path}.{k}", out)
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _walk_forbidden(v, f"{path}[{i}]", out)


def validate_definition(d: WorkflowDefinition) -> list[str]:
    errors: list[str] = []
    ids = [s.step_id for s in d.steps]
    if len(ids) != len(set(ids)):
        errors.append("duplicate step ids")
    if d.start_step not in ids:
        errors.append(f"start_step {d.start_step!r} is not a step")
    if not any(s.type is StepType.END for s in d.steps):
        errors.append("a workflow needs an END step")
    for s in d.steps:
        missing = _REQUIRED[s.type] - set(s.config)
        if missing:
            errors.append(f"step {s.step_id}: missing {sorted(missing)}")
        _walk_forbidden(s.config, f"step {s.step_id}", errors)
        targets = [s.next] + ([s.config.get("on_true"), s.config.get("on_false")] if s.type is StepType.CONDITION else [])
        for t in targets:
            if t is not None and t not in ids:
                errors.append(f"step {s.step_id}: refers to unknown step {t!r}")
        if s.type not in (StepType.END, StepType.CONDITION) and s.next is None:
            errors.append(f"step {s.step_id}: needs `next`")
    return errors


def _resolve(state: Mapping[str, Any], ref: str) -> Any:
    node: Any = state
    for part in ref.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    return node


@dataclass
class WorkflowEngine:
    cc: Any
    runtime: Any = None
    definitions: Repository[WorkflowDefinition] = field(default_factory=lambda: InMemoryRepository("workflow_id"))
    runs: Repository[WorkflowRun] = field(default_factory=lambda: InMemoryRepository("workflow_run_id"))
    max_steps_per_advance: int = 100

    # -- definitions ----------------------------------------------------------------------
    def create_definition(self, d: WorkflowDefinition) -> WorkflowDefinition:
        errors = validate_definition(d)
        if errors:
            raise ValidationFailedError("invalid workflow definition", errors)
        if d.organization_id != self.cc.policy.organization_id:
            raise PermissionDeniedError("workflow belongs to a different organization")
        self.definitions.add(replace(d, status=WorkflowStatus.DRAFT))
        self._audit(EventType.WORKFLOW_DEFINITION_CHANGED, d.workflow_id, ActorType.HUMAN, d.created_by,
                    {"action": "created", "definition_version": d.definition_version}, new_state=WorkflowStatus.DRAFT)
        return self.definitions.get(d.workflow_id)

    def set_status(self, workflow_id: str, target: WorkflowStatus, principal: Principal) -> WorkflowDefinition:
        if not principal.is_human or principal.organization_id != self.cc.policy.organization_id:
            raise PermissionDeniedError("only a human of the organization can change workflow status")
        d = self.definitions.get(workflow_id)
        WORKFLOW_MACHINE.check(d.status, target)
        if target is WorkflowStatus.ACTIVE:
            errors = validate_definition(d)
            if errors:
                raise ValidationFailedError("cannot activate an invalid workflow", errors)
        self.definitions.update(replace(d, status=target), expected_version=d.version)
        self._audit(EventType.WORKFLOW_DEFINITION_CHANGED, workflow_id, ActorType.HUMAN, principal.principal_id,
                    {"action": "status"}, previous_state=d.status, new_state=target)
        return self.definitions.get(workflow_id)

    # -- runs ------------------------------------------------------------------------------
    def start(self, workflow_id: str, principal: Principal, inputs: Mapping[str, Any] | None = None) -> WorkflowRun:
        d = self.definitions.get(workflow_id)
        if d.status is not WorkflowStatus.ACTIVE:
            raise PolicyViolationError(f"workflow is {d.status.value}; only ACTIVE workflows run")
        if not principal.is_human or principal.organization_id != d.organization_id:
            raise PermissionDeniedError("workflows run on behalf of a human of the same organization")
        run = WorkflowRun(new_id("wfr"), d.organization_id, d.workflow_id, d.definition_version, principal,
                          WorkflowRunStatus.QUEUED, d.start_step, {"inputs": dict(inputs or {}), "steps": {}},
                          self.cc.clock())
        self.runs.add(run)
        self._audit(EventType.WORKFLOW_STARTED, run.workflow_run_id, ActorType.HUMAN, principal.principal_id,
                    {"workflow_id": workflow_id, "definition_version": d.definition_version},
                    new_state=WorkflowRunStatus.QUEUED,
                    object_type="workflow_run")
        return self.advance(run.workflow_run_id)

    def _save(self, run: WorkflowRun, **changes: Any) -> WorkflowRun:
        if "status" in changes and changes["status"] is not run.status:
            WORKFLOW_RUN_MACHINE.check(run.status, changes["status"])
        return self.runs.update(replace(run, **changes), expected_version=run.version)

    def _step_state(self, run: WorkflowRun, step_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        state = {"inputs": run.state.get("inputs", {}), "steps": {**run.state.get("steps", {}), step_id: dict(value)}}
        return state

    def advance(self, workflow_run_id: str) -> WorkflowRun:
        run = self.runs.get(workflow_run_id)
        if WORKFLOW_RUN_MACHINE.is_terminal(run.status):
            return run
        if run.status is WorkflowRunStatus.WAITING:
            resumed = self._check_wait(run)
            if resumed is None:
                return run
            run = resumed
        if run.status is not WorkflowRunStatus.RUNNING:
            run = self._save(run, status=WorkflowRunStatus.RUNNING)
        d = self.definitions.get(run.workflow_id)
        for _ in range(self.max_steps_per_advance):
            step = d.step(run.current_step)
            try:
                run, outcome = self._execute(run, step)
            except Exception as exc:  # a broken step fails the workflow; it never half-succeeds silently
                return self._finish(run, WorkflowRunStatus.FAILED, f"step {step.step_id}: {type(exc).__name__}: {exc}")
            if outcome == "END":
                return self._finish(run, WorkflowRunStatus.SUCCEEDED)
            if outcome == "WAIT":
                self._audit(EventType.WORKFLOW_WAITING, run.workflow_run_id, ActorType.SYSTEM, "workflow",
                            dict(run.waiting_on or {}), object_type="workflow_run")
                return run
            if outcome.startswith("FAIL:"):
                return self._finish(run, WorkflowRunStatus.FAILED, outcome[5:])
            run = self._save(run, current_step=outcome)
        return self._finish(run, WorkflowRunStatus.FAILED, "step limit exceeded (possible loop)")

    def _finish(self, run: WorkflowRun, status: WorkflowRunStatus, error: str | None = None) -> WorkflowRun:
        run = self._save(run, status=status, ended_at=self.cc.clock(), error=error, waiting_on=None)
        self._audit(EventType.WORKFLOW_COMPLETED if status is WorkflowRunStatus.SUCCEEDED else EventType.WORKFLOW_FAILED,
                    run.workflow_run_id, ActorType.SYSTEM, "workflow", {"error": error}, new_state=status,
                    object_type="workflow_run")
        return run

    def _wait(self, run: WorkflowRun, waiting_on: Mapping[str, Any]) -> tuple[WorkflowRun, str]:
        return self._save(run, status=WorkflowRunStatus.WAITING, waiting_on=dict(waiting_on)), "WAIT"

    def _check_wait(self, run: WorkflowRun) -> WorkflowRun | None:
        w = run.waiting_on or {}
        d = self.definitions.get(run.workflow_id)
        step = d.step(run.current_step)
        kind = w.get("type")
        if kind == "approval":
            a = self.cc.approvals.get(w["approval_id"])
            if a.status.value == "PENDING":
                return None
            if a.status.value != "APPROVED":
                self._finish(run, WorkflowRunStatus.FAILED, f"approval {a.approval_id} is {a.status.value}")
                return None
            outcome = self.cc.execute_approved(a.approval_id, tool_name=a.tool_name, action_type=a.action_type,
                                               payload=dict(a.payload))
            if outcome.status is not ActionStatus.EXECUTED:
                self._finish(run, WorkflowRunStatus.FAILED, f"approved action {outcome.status.value}: {outcome.reason}")
                return None
            state = self._step_state(run, step.step_id, {"status": "EXECUTED", "approval_id": a.approval_id,
                                                         "result_ref": outcome.result.result_ref})
        elif kind == "gate":
            if not w.get("approved_by"):
                return None
            state = self._step_state(run, step.step_id, {"status": "APPROVED", "approved_by": w["approved_by"]})
        elif kind == "task":
            t = self.cc.tasks.get(w["task_id"])
            if t.status is TaskStatus.CANCELLED:
                self._finish(run, WorkflowRunStatus.FAILED, f"human task {t.task_id} was cancelled")
                return None
            if t.status is not TaskStatus.COMPLETED:
                return None
            state = self._step_state(run, step.step_id, {"status": "COMPLETED", "task_id": t.task_id})
        elif kind == "delay":
            if self.cc.clock() < datetime.fromisoformat(w["until"]):
                return None
            state = self._step_state(run, step.step_id, {"status": "ELAPSED"})
        else:
            return None
        return self._save(run, status=WorkflowRunStatus.RUNNING, waiting_on=None, state=state,
                          current_step=step.next)

    def approve_gate(self, workflow_run_id: str, principal: Principal) -> WorkflowRun:
        run = self.runs.get(workflow_run_id)
        w = run.waiting_on or {}
        if run.status is not WorkflowRunStatus.WAITING or w.get("type") != "gate":
            raise InvalidTransitionError("workflow is not waiting at an approval gate")
        if not principal.is_human or principal.organization_id != run.organization_id \
                or w["required_role"] not in principal.roles:
            raise PermissionDeniedError(f"gate requires a human with role {w['required_role']!r}")
        if principal.principal_id == run.principal.principal_id and w.get("separate_approver", True):
            raise PermissionDeniedError("the workflow's initiator cannot approve its own gate")
        self._save(run, waiting_on={**w, "approved_by": principal.principal_id})
        return self.advance(workflow_run_id)

    # -- step execution -----------------------------------------------------------------
    def _agent_run(self, run: WorkflowRun, agent_id: str, objective: str):
        r = self.cc.runs.create_run(agent_id, run.principal, objective)
        return self.cc.runs.start(r.run_id)

    def _execute(self, run: WorkflowRun, step: WorkflowStep) -> tuple[WorkflowRun, str]:
        c = step.config
        self._audit(EventType.WORKFLOW_STEP, run.workflow_run_id, ActorType.SYSTEM, "workflow",
                    {"step_id": step.step_id, "type": step.type.value}, object_type="workflow_run")
        if step.type is StepType.END:
            return run, "END"
        if step.type is StepType.AGENT:
            if self.runtime is None:
                return run, "FAIL:no agent runtime configured"
            ar = self._agent_run(run, c["agent_id"], c["objective"])
            ctx = RunContext(c.get("organization_context", "Workflow step"), (), (), c["objective"], run.principal,
                             workflow_run_id=run.workflow_run_id, requested_agent_id=c["agent_id"])
            res = self.runtime.execute(ar.run_id, ctx, message=c.get("message"))
            ok = res.status in (RunStatus.SUCCEEDED, RunStatus.APPROVAL_REQUIRED)
            run = self._save(run, state=self._step_state(run, step.step_id, {
                "status": res.status.value, "run_id": ar.run_id, "output_id": res.output_id, "error": res.error}))
            if not ok and not c.get("continue_on_failure", False):
                return run, f"FAIL:agent step {step.step_id} {res.status.value}: {res.error}"
            return run, step.next
        if step.type in (StepType.TOOL, StepType.KPI_UPDATE):
            ar = self._agent_run(run, c["agent_id"], f"workflow {run.workflow_id} step {step.step_id}")
            if step.type is StepType.TOOL:
                action = {"tool": c["tool"], "action_type": c.get("action_type", c["tool"]),
                          "parameters": dict(c["parameters"]), "reason": c.get("reason", f"workflow step {step.step_id}")}
            else:
                action = {"tool": "record_kpi_observation", "action_type": "record_kpi_observation",
                          "parameters": {k: c[k] for k in ("kpi", "value", "unit", "observed_at", "source_reference")},
                          "reason": f"workflow KPI update {step.step_id}"}
            outcome = self.cc.gateway.submit(ar.run_id, action)   # the gateway decides risk and approval
            if outcome.status is ActionStatus.PENDING_APPROVAL:
                run = self._save(run, state=self._step_state(run, step.step_id, {
                    "status": "PENDING_APPROVAL", "approval_id": outcome.approval_id,
                    "risk_class": outcome.assessment.final_class.value, "run_id": ar.run_id}))
                return self._wait(run, {"type": "approval", "approval_id": outcome.approval_id})
            run = self._save(run, state=self._step_state(run, step.step_id, {
                "status": outcome.status.value, "run_id": ar.run_id, "reason": outcome.reason}))
            self._close_run(ar.run_id, outcome.status is ActionStatus.EXECUTED)
            if outcome.status is not ActionStatus.EXECUTED:
                return run, f"FAIL:tool step {step.step_id} {outcome.status.value}: {outcome.reason}"
            return run, step.next
        if step.type is StepType.CONDITION:
            return run, c["on_true"] if _resolve(run.state, c["ref"]) == c["equals"] else c["on_false"]
        if step.type is StepType.APPROVAL:
            return self._wait(run, {"type": "gate", "required_role": c["required_role"], "summary": c["summary"],
                                    "separate_approver": c.get("separate_approver", True)})
        if step.type is StepType.HUMAN_TASK:
            task = self.cc.tasks.create_from_contract({
                "title": c["title"], "objective": c.get("objective", c["title"]), "description": c.get("description") or f"Workflow {run.workflow_id} step {step.step_id}: {c['title']}",
                "owner": f"human:{c['owner_role']}", "priority": c.get("priority", "P2"), "dependencies": [],
                "expected_output": c.get("expected_output", "Task completed"),
                "completion_criteria": list(c["completion_criteria"]), "kpi_affected": [], "risk_level": "GREEN"},
                organization_id=run.organization_id, created_by="workflow", actor_type=ActorType.SYSTEM,
                owner_type=ActorType.HUMAN)
            return self._wait(run, {"type": "task", "task_id": task.task_id})
        if step.type is StepType.NOTIFICATION:
            n = self.cc.alerts.notify(organization_id=run.organization_id, recipient=c["recipient"],
                                      subject=c["subject"], body=c["body"])
            run = self._save(run, state=self._step_state(run, step.step_id, {"notification_id": n.notification_id}))
            return run, step.next
        if step.type is StepType.DELAY:
            until = self.cc.clock() + timedelta(minutes=float(c["minutes"]))
            return self._wait(run, {"type": "delay", "until": until.isoformat()})
        return run, f"FAIL:unsupported step type {step.type}"

    def _close_run(self, run_id: str, ok: bool) -> None:
        r = self.cc.runs.get(run_id)
        if r.status is RunStatus.RUNNING:
            self.cc.runs.transition(run_id, RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
                                    reason=None if ok else "workflow tool step did not execute")
        elif not RUN_MACHINE.is_terminal(r.status) and r.status is RunStatus.APPROVAL_REQUIRED and not ok:
            self.cc.runs.transition(run_id, RunStatus.FAILED, reason="workflow tool step did not execute")

    def _audit(self, event: EventType, object_id: str, actor_type: ActorType, actor_id: str,
               payload: Mapping[str, Any] | None = None, previous_state: Any = None, new_state: Any = None,
               object_type: str = "workflow") -> None:
        self.cc.audit.record(event, organization_id=self.cc.policy.organization_id, actor_type=actor_type,
                             actor_id=actor_id, object_type=object_type, object_id=object_id, payload=payload,
                             previous_state=previous_state, new_state=new_state)
