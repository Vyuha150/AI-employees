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
from .common import ActorType, Clock, StrEnum, new_id, utc_now
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
    tool_call_id: str | None = None
    # Stable identifier for one logical execution. Adapters that support idempotency must
    # de-duplicate on it (for approved actions it is the approval id).
    execution_id: str | None = None
    # The human the run acts for. Read connectors use it for permission filtering (e.g. knowledge).
    principal: Any = None


class ToolStatus(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    ERROR = "ERROR"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"   # external call outcome cannot be determined (e.g. timeout)
    OUTCOME_UNKNOWN = "EXECUTION_UNKNOWN"     # alias (Part 55 name)


_SUCCESS_STATUSES = frozenset({ToolStatus.OK, ToolStatus.PARTIAL, ToolStatus.STALE})


@dataclass(frozen=True)
class ToolResult:
    """Tool result contract (Part 35). `success` is kept for compatibility and always agrees
    with `status`: OK / PARTIAL / STALE are successes; UNAVAILABLE / ERROR / EXECUTION_UNKNOWN
    are not. `mock` is true whenever the result was simulated."""

    success: bool
    result_ref: str | None = None
    data: Mapping[str, Any] | None = None
    error: str | None = None
    status: ToolStatus | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    source_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    executed_at: datetime | None = None
    mock: bool = False
    freshness: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        status = self.status or (ToolStatus.OK if self.success else ToolStatus.ERROR)
        object.__setattr__(self, "status", status)
        if (status in _SUCCESS_STATUSES) != self.success:
            raise ValueError(f"ToolResult success={self.success} contradicts status {status.value}")
        if isinstance(self.data, Mapping) and self.data.get("mock"):
            object.__setattr__(self, "mock", True)

    def for_model(self) -> dict[str, Any]:
        """What the model sees. Simulated results are labelled so they are never mistaken for reality."""
        out: dict[str, Any] = {
            "tool_call_id": self.tool_call_id, "tool_name": self.tool_name, "status": self.status.value,
            "data": self.data, "source_ids": list(self.source_ids), "warnings": list(self.warnings),
            "error": self.error, "freshness": self.freshness, "limitations": list(self.limitations),
            "executed_at": self.executed_at.isoformat() if self.executed_at else None, "mock": self.mock,
        }
        if self.mock:
            out["notice"] = ("SIMULATED RESULT: this tool is mocked. Nothing happened in any real system. "
                             "Do not report it as a real business action.")
        return out


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
    alerts: Any = None   # AlertService; when set, create_alert creates a real alert record
    kpis: Any = None     # KpiService; when set, observations are stored as KPI snapshots

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
                if self.alerts is not None:
                    p = inv.payload
                    alert = self.alerts.create(
                        organization_id=inv.organization_id, severity=p["severity"], category=p.get("category", "AGENT"),
                        title=p["title"], description=p["detail"], source_agent=inv.agent_id,
                        source_ids=p.get("source_ids", ()), related_object_type=p.get("related_object_type"),
                        related_object_id=p.get("related_object_id"), actor_type=ActorType.AGENT,
                        actor_id=inv.agent_id, run_id=inv.run_id,
                        dedupe_key=f"agent:{inv.agent_id}:{p['title']}")
                    return ToolResult(True, alert.alert_id)
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
                if self.kpis is not None:
                    snap = self.kpis.record(p, organization_id=inv.organization_id, recorded_by=inv.agent_id,
                                            run_id=inv.run_id)
                    obs = KpiObservation(snap.snapshot_id, inv.organization_id, p["kpi"], str(p["value"]), p["unit"],
                                         str(p["observed_at"]), p["source_reference"], inv.agent_id, inv.run_id,
                                         self.clock())
                self.kpi_observations.append(obs)
                return ToolResult(True, obs.observation_id)
        except (CommandCenterError, KeyError, ValueError) as exc:
            return ToolResult(False, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(False, error=f"internal tool {inv.tool_name!r} has no executor")


@dataclass
class MockExternalExecutor:
    """Records external/high-risk calls. Never performs a real side effect.

    De-duplicates on execution_id: a repeated execution id returns the first result and is
    not recorded as a second call (models an idempotent adapter)."""

    calls: list[ToolInvocation] = field(default_factory=list)
    fail_tools: set[str] = field(default_factory=set)
    timeout_tools: set[str] = field(default_factory=set)
    _by_execution: dict[str, ToolResult] = field(default_factory=dict)

    def execute(self, inv: ToolInvocation) -> ToolResult:
        if inv.execution_id and inv.execution_id in self._by_execution:
            return self._by_execution[inv.execution_id]
        self.calls.append(inv)
        if inv.tool_name in self.timeout_tools:
            raise TimeoutError("mock external call timed out (configured)")
        if inv.tool_name in self.fail_tools:
            result = ToolResult(False, error="mock failure (configured)", mock=True)
        else:
            result = ToolResult(True, f"mock:{new_id('ext')}", {"mock": True}, mock=True)
        if inv.execution_id:
            self._by_execution[inv.execution_id] = result
        return result


@dataclass
class CompositeExecutor:
    tools: ToolRegistry
    internal: InternalToolExecutor
    external: ToolExecutor
    data_provider: DataProvider | None = None
    read_tools: Any = None   # ReadToolService (read_tools.py); preferred over data_provider

    def execute(self, inv: ToolInvocation) -> ToolResult:
        spec = self.tools.get(inv.tool_name)
        if spec.category is ToolCategory.READ:
            if self.read_tools is not None and self.read_tools.supports(inv.tool_name):
                return self.read_tools.execute(inv)
            if self.data_provider is None:
                return ToolResult(False, error="no data adapter configured; data unavailable",
                                  status=ToolStatus.UNAVAILABLE,
                                  limitations=("no connector is configured for this dataset",))
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
