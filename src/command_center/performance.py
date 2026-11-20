"""Agent performance aggregation (Part 15).

Computed only from the audit log, so every number traces to recorded events. A metric
whose inputs do not exist is None ("not measurable"), never 0 and never estimated.

Metric definitions (fixed; changing one is a configuration change):
  tasks_assigned             task.created events whose payload.owner is the agent
  tasks_completed            task.updated -> COMPLETED whose payload.owner is the agent
  completion_rate            tasks_completed / tasks_assigned
  output_acceptance_rate     output.reviewed APPROVED / output.reviewed
  revision_rate              output.reviewed NEEDS_REVISION / output.reviewed
  rejection_rate             output.reviewed REJECTED / output.reviewed
  recommendations_implemented recommendation.updated -> IMPLEMENTED for the source agent
  business_outcomes_verified recommendation.updated -> VERIFIED for the source agent
  average_quality_score      mean composite_score of output.reviewed
  tool_failure_rate          tool.failed / (tool.executed + tool.failed)
  run_failure_rate           agent_run.failed / (agent_run.completed + agent_run.failed)
  estimated_cost             sum of cost_used reported on finished runs (None if none reported)
  cost_per_completed_task    estimated_cost / tasks_completed
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .audit import AuditStore, EventType
from .common import StrEnum


class Period(StrEnum):
    D7 = "7d"
    D30 = "30d"
    D90 = "90d"

    @property
    def days(self) -> int:
        return int(self.value[:-1])


@dataclass(frozen=True)
class AgentPerformance:
    agent_id: str
    period: Period
    period_start: datetime
    period_end: datetime
    tasks_assigned: int
    tasks_completed: int
    completion_rate: float | None
    outputs_reviewed: int
    output_acceptance_rate: float | None
    revision_rate: float | None
    rejection_rate: float | None
    recommendations_implemented: int
    business_outcomes_verified: int
    average_quality_score: float | None
    tool_failure_rate: float | None
    run_failure_rate: float | None
    estimated_cost: float | None
    cost_per_completed_task: float | None


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def compute_agent_performance(store: AuditStore, agent_id: str, period: Period, now: datetime) -> AgentPerformance:
    start = now - timedelta(days=period.days)
    events = [e for e in store.events() if start <= e.timestamp <= now]

    def owned(e) -> bool:
        return e.payload.get("owner") == agent_id

    def by_agent(e) -> bool:
        return e.agent_id == agent_id

    assigned = sum(1 for e in events if e.event_type is EventType.TASK_CREATED and owned(e))
    completed = sum(1 for e in events if e.event_type is EventType.TASK_UPDATED and e.new_state == "COMPLETED"
                    and owned(e))
    reviews = [e for e in events if e.event_type is EventType.OUTPUT_REVIEWED and by_agent(e)]
    decisions = [e.payload.get("decision") for e in reviews]
    scores = [Decimal(e.payload["composite_score"]) for e in reviews if "composite_score" in e.payload]
    rec_updates = [e for e in events if e.event_type is EventType.RECOMMENDATION_UPDATED and by_agent(e)]
    tool_ok = sum(1 for e in events if e.event_type is EventType.TOOL_EXECUTED and by_agent(e))
    tool_bad = sum(1 for e in events if e.event_type is EventType.TOOL_FAILED and by_agent(e))
    finished = [e for e in events if e.event_type in (EventType.AGENT_RUN_COMPLETED, EventType.AGENT_RUN_FAILED)
                and by_agent(e)]
    runs_failed = sum(1 for e in finished if e.event_type is EventType.AGENT_RUN_FAILED)
    costs = [float(e.payload["cost_used"]) for e in finished if "cost_used" in e.payload]
    cost = sum(costs) if costs else None

    return AgentPerformance(
        agent_id=agent_id, period=period, period_start=start, period_end=now,
        tasks_assigned=assigned, tasks_completed=completed, completion_rate=_rate(completed, assigned),
        outputs_reviewed=len(reviews),
        output_acceptance_rate=_rate(decisions.count("APPROVED"), len(reviews)),
        revision_rate=_rate(decisions.count("NEEDS_REVISION"), len(reviews)),
        rejection_rate=_rate(decisions.count("REJECTED"), len(reviews)),
        recommendations_implemented=sum(1 for e in rec_updates if e.new_state == "IMPLEMENTED"),
        business_outcomes_verified=sum(1 for e in rec_updates if e.new_state == "VERIFIED"),
        average_quality_score=float(sum(scores) / len(scores)) if scores else None,
        tool_failure_rate=_rate(tool_bad, tool_ok + tool_bad),
        run_failure_rate=_rate(runs_failed, len(finished)),
        estimated_cost=cost,
        cost_per_completed_task=None if cost is None or completed == 0 else cost / completed,
    )
