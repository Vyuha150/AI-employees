"""Tasks (Part 10) following the §8 task standard."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Mapping

from .audit import AuditLogger, EventType
from .common import ActorType, Clock, RiskClass, StrEnum, new_id, utc_now
from .errors import ValidationFailedError
from .state_machines import TASK_MACHINE, TaskStatus
from .storage import InMemoryRepository, Repository


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


@dataclass(frozen=True)
class Task:
    task_id: str
    organization_id: str
    title: str
    objective: str
    description: str
    owner: str
    owner_type: ActorType
    priority: Priority
    dependencies: tuple[str, ...]
    expected_output: str
    completion_criteria: tuple[str, ...]
    kpi_affected: tuple[str, ...]
    risk_level: RiskClass
    created_by: str
    created_at: datetime
    due_date: date | None = None
    source_run_id: str | None = None
    status: TaskStatus = TaskStatus.BACKLOG
    blocked_reason: str | None = None
    version: int = 0


@dataclass
class TaskService:
    audit: AuditLogger
    repo: Repository[Task] = field(default_factory=lambda: InMemoryRepository("task_id"))
    clock: Clock = utc_now

    def get(self, task_id: str) -> Task:
        return self.repo.get(task_id)

    def create_from_contract(self, data: Mapping[str, Any], *, organization_id: str, created_by: str,
                             actor_type: ActorType, agent_id: str | None = None,
                             run_id: str | None = None, owner_type: ActorType = ActorType.AGENT) -> Task:
        errors = []
        for key in ("title", "objective", "description", "owner", "expected_output"):
            if not str(data.get(key, "")).strip():
                errors.append(f"{key} is required")
        if not data.get("completion_criteria"):
            errors.append("completion_criteria must not be empty (no meaningless microtasks)")
        try:
            priority = Priority(data.get("priority"))
            risk = RiskClass(data.get("risk_level"))
        except ValueError as exc:
            errors.append(str(exc))
        due = data.get("due_date")
        if due is not None and not isinstance(due, date):
            try:
                due = date.fromisoformat(str(due))
            except ValueError:
                errors.append("due_date must be an ISO date")
        if errors:
            raise ValidationFailedError("invalid task", errors)

        task = Task(task_id=new_id("task"), organization_id=organization_id, title=data["title"],
                    objective=data["objective"], description=data["description"], owner=data["owner"],
                    owner_type=owner_type, priority=priority, dependencies=tuple(data.get("dependencies", ())),
                    expected_output=data["expected_output"],
                    completion_criteria=tuple(data["completion_criteria"]),
                    kpi_affected=tuple(data.get("kpi_affected", ())), risk_level=risk, created_by=created_by,
                    created_at=self.clock(), due_date=due, source_run_id=run_id)
        for dep in task.dependencies:
            self.repo.get(dep)  # dependencies must exist
        self.repo.add(task)
        self.audit.record(EventType.TASK_CREATED, organization_id=organization_id, actor_type=actor_type,
                          actor_id=created_by, agent_id=agent_id, run_id=run_id, object_type="task",
                          object_id=task.task_id, payload={"title": task.title, "owner": task.owner,
                                                           "priority": priority.value},
                          new_state=task.status)
        return task

    def transition(self, task_id: str, target: TaskStatus, *, actor_type: ActorType, actor_id: str,
                   agent_id: str | None = None, run_id: str | None = None, reason: str | None = None) -> Task:
        task = self.repo.get(task_id)
        TASK_MACHINE.check(task.status, target)
        if target is TaskStatus.BLOCKED and not (reason or "").strip():
            raise ValidationFailedError("blocking a task requires a reason")
        if target is TaskStatus.COMPLETED:
            open_deps = [d for d in task.dependencies if self.repo.get(d).status is not TaskStatus.COMPLETED]
            if open_deps:
                raise ValidationFailedError("cannot complete a task with open dependencies", open_deps)
        updated = self.repo.update(
            replace(task, status=target, blocked_reason=reason if target is TaskStatus.BLOCKED else None),
            expected_version=task.version)
        self.audit.record(EventType.TASK_UPDATED, organization_id=task.organization_id, actor_type=actor_type,
                          actor_id=actor_id, agent_id=agent_id or (task.owner if task.owner_type is ActorType.AGENT else None),
                          run_id=run_id, object_type="task", object_id=task_id,
                          payload={"reason": reason, "owner": task.owner} if reason else {"owner": task.owner},
                          previous_state=task.status, new_state=target)
        return updated

    def overdue(self, today: date, priorities: tuple[Priority, ...] = (Priority.P0, Priority.P1)) -> list[Task]:
        return self.repo.list(lambda t: t.due_date is not None and t.due_date < today and t.priority in priorities
                              and not TASK_MACHINE.is_terminal(t.status))
