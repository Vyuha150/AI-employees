"""Tool executors. The gateway is the only caller.

- InternalToolExecutor: Command Center records (tasks, alerts, KPI observations, change
  request drafts). These are real, internal, reversible state changes.
- Read tools: served by a DataProvider. Without one, a read FAILS with "no data adapter
  configured"; it never returns invented data.
- MockExternalExecutor: external and high-risk tools are NOT implemented. The mock records
  the call and returns a result explicitly marked {"mock": true}. It is for tests and
  dry runs only and must be replaced by real, separately reviewed adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from .audit import AuditLogger, EventType
from .common import ActorType, Clock, new_id, utc_now
from .errors import CommandCenterError, ValidationFailedError
from .state_machines import TaskStatus
from .tasks import TaskService
from .tools import ToolCategory, ToolRegistry


@dataclass(frozen=True)
class ToolInvocation:
    organization_id: str
    agent_id: str
    run_id: str
    tool_name: str
    action_type: str
    payload: Mapping[str, Any]
    approval_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    success: bool
    result_ref: str | None = None
    data: Mapping[str, Any] | None = None
    error: str | None = None


class ToolExecutor(Protocol):
    def execute(self, invocation: ToolInvocation) -> ToolResult: ...


DataProvider = Callable[[ToolInvocation], Mapping[str, Any]]


@dataclass(frozen=True)
class KpiObservation:
    observation_id: str
    organization_id: str
    kpi: str
    value: str
    unit: str
    observed_at: str
    source_reference: str
    recorded_by_agent: str
    run_id: str
    recorded_at: datetime


@dataclass
class InternalToolExecutor:
    tasks: TaskService
    audit: AuditLogger
    clock: Clock = utc_now
    kpi_observations: list[KpiObservation] = field(default_factory=list)

    HANDLED = frozenset({"create_task", "update_task_status", "create_alert", "record_kpi_observation"})

    def execute(self, inv: ToolInvocation) -> ToolResult:
        try:
            if inv.tool_name == "create_task":
                task = self.tasks.create_from_contract(inv.payload, organization_id=inv.organization_id,
                                                       created_by=inv.agent_id, actor_type=ActorType.AGENT,
                                                       agent_id=inv.agent_id, run_id=inv.run_id)
                return ToolResult(True, task.task_id, {"task_id": task.task_id})
            if inv.tool_name == "update_task_status":
                task = self.tasks.transition(inv.payload["task_id"], TaskStatus(inv.payload["status"]),
                                             actor_type=ActorType.AGENT, actor_id=inv.agent_id,
                                             run_id=inv.run_id, reason=inv.payload.get("reason"))
                return ToolResult(True, task.task_id, {"status": task.status.value})
            if inv.tool_name == "create_alert":
                for k in ("severity", "title", "detail"):
                    if not inv.payload.get(k):
                        raise ValidationFailedError(f"alert needs {k}")
                alert_id = new_id("alr")
                self.audit.record(EventType.ALERT_CREATED, organization_id=inv.organization_id,
                                  actor_type=ActorType.AGENT, actor_id=inv.agent_id, agent_id=inv.agent_id,
                                  run_id=inv.run_id, object_type="alert", object_id=alert_id, payload=inv.payload)
                return ToolResult(True, alert_id)
            if inv.tool_name == "record_kpi_observation":
                p = inv.payload
                missing = [k for k in ("kpi", "value", "unit", "observed_at", "source_reference") if not p.get(k)]
                if missing:
                    raise ValidationFailedError("KPI observation incomplete (a source is mandatory)", missing)
                obs = KpiObservation(new_id("kpi"), inv.organization_id, p["kpi"], str(p["value"]), p["unit"],
                                     str(p["observed_at"]), p["source_reference"], inv.agent_id, inv.run_id,
                                     self.clock())
                self.kpi_observations.append(obs)
                return ToolResult(True, obs.observation_id)
        except (CommandCenterError, KeyError, ValueError) as exc:
            return ToolResult(False, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(False, error=f"internal tool {inv.tool_name!r} has no executor")


@dataclass
class MockExternalExecutor:
    """Records external/high-risk calls. Never performs a real side effect."""

    calls: list[ToolInvocation] = field(default_factory=list)
    fail_tools: set[str] = field(default_factory=set)

    def execute(self, inv: ToolInvocation) -> ToolResult:
        self.calls.append(inv)
        if inv.tool_name in self.fail_tools:
            return ToolResult(False, error="mock failure (configured)")
        return ToolResult(True, f"mock:{new_id('ext')}", {"mock": True})


@dataclass
class CompositeExecutor:
    tools: ToolRegistry
    internal: InternalToolExecutor
    external: ToolExecutor
    data_provider: DataProvider | None = None

    def execute(self, inv: ToolInvocation) -> ToolResult:
        spec = self.tools.get(inv.tool_name)
        if spec.category is ToolCategory.READ:
            if self.data_provider is None:
                return ToolResult(False, error="no data adapter configured; data unavailable")
            try:
                return ToolResult(True, f"read:{new_id('rd')}", dict(self.data_provider(inv)))
            except Exception as exc:  # adapter failures are reported, not hidden
                return ToolResult(False, error=f"data adapter failed: {exc}")
        if inv.tool_name in InternalToolExecutor.HANDLED:
            return self.internal.execute(inv)
        if spec.category in (ToolCategory.EXTERNAL, ToolCategory.HIGH_RISK):
            return self.external.execute(inv)
        return ToolResult(False, error=f"tool {inv.tool_name!r} is not executable through the gateway "
                                       "(handled by its dedicated service)")
