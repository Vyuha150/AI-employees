"""Read-tool interfaces (Part 34).

Every read returns a ToolResult with status (OK | PARTIAL | UNAVAILABLE | STALE | ERROR),
data, source_ids (one per returned record), freshness and limitations. A tool without a
configured connector returns UNAVAILABLE; nothing is ever synthesized.

Connectors shipped here read the Command Center's own records (KPI snapshots, tasks,
knowledge). PostgreSQL connectors (command_center.pg.read_connectors) read the database,
including organization-provided connector views for customers, leads, opportunities,
projects, vendors and financial summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol

from .common import Clock, new_id, utc_now
from .executors import ToolInvocation, ToolResult, ToolStatus

READ_TOOLS = ("read_kpis", "read_tasks", "read_projects", "read_customers", "read_leads", "read_opportunities",
              "read_financial_summary", "read_vendors", "read_knowledge", "search_company_data")

DEFAULT_ROW_LIMIT = 50


class ReadConnector(Protocol):
    def fetch(self, inv: ToolInvocation) -> ToolResult: ...


def unavailable(inv: ToolInvocation, why: str) -> ToolResult:
    return ToolResult(False, error=why, status=ToolStatus.UNAVAILABLE, tool_name=inv.tool_name,
                      limitations=(why,))


def rows_result(inv: ToolInvocation, rows: list[Mapping[str, Any]], source_ids: list[str], *,
                freshness: datetime | None, max_age: timedelta | None, limit: int, now: datetime,
                extra_limitations: tuple[str, ...] = ()) -> ToolResult:
    limitations = list(extra_limitations)
    status = ToolStatus.OK
    if len(rows) >= limit:
        status = ToolStatus.PARTIAL
        limitations.append(f"result truncated at {limit} records")
    if freshness is not None and max_age is not None and now - freshness > max_age:
        status = ToolStatus.STALE
        limitations.append(f"newest record is from {freshness.isoformat()}, older than {max_age}")
    return ToolResult(True, f"read:{new_id('rd')}", {"records": rows, "count": len(rows)}, status=status,
                      tool_name=inv.tool_name, source_ids=tuple(source_ids),
                      freshness=freshness.isoformat() if freshness else None, limitations=tuple(limitations))


@dataclass
class ReadToolService:
    connectors: dict[str, ReadConnector] = field(default_factory=dict)

    def supports(self, tool_name: str) -> bool:
        return tool_name in self.connectors

    def execute(self, inv: ToolInvocation) -> ToolResult:
        conn = self.connectors.get(inv.tool_name)
        if conn is None:
            return unavailable(inv, f"no connector configured for {inv.tool_name}; data unavailable")
        try:
            return conn.fetch(inv)
        except Exception as exc:  # report, never hide
            return ToolResult(False, error=f"connector error: {type(exc).__name__}: {exc}", status=ToolStatus.ERROR,
                              tool_name=inv.tool_name)


# ---------------------------------------------------------------------------------------
# connectors over the Command Center's own records
# ---------------------------------------------------------------------------------------
@dataclass
class KpiReadConnector:
    kpis: Any                                   # KpiService
    max_age: timedelta | None = timedelta(days=7)
    clock: Clock = utc_now

    def fetch(self, inv: ToolInvocation) -> ToolResult:
        wanted = inv.payload.get("kpis")
        limit = min(int(inv.payload.get("limit", DEFAULT_ROW_LIMIT)), DEFAULT_ROW_LIMIT)
        names = list(wanted) if wanted else self.kpis.list_kpis(inv.organization_id)
        rows, ids, newest = [], [], None
        for name in names:
            for snap in self.kpis.history(inv.organization_id, name, limit):
                rows.append({"kpi": snap.kpi, "value": str(snap.value), "unit": snap.unit,
                             "observed_at": snap.observed_at, "source_reference": snap.source_reference})
                ids.append(f"kpi:{snap.snapshot_id}")
                newest = snap.recorded_at if newest is None or snap.recorded_at > newest else newest
        if not rows:
            return unavailable(inv, "no KPI observations recorded for the requested KPIs")
        return rows_result(inv, rows[:limit], ids[:limit], freshness=newest, max_age=self.max_age, limit=limit,
                           now=self.clock())


@dataclass
class TaskReadConnector:
    tasks: Any                                  # TaskService
    clock: Clock = utc_now

    def fetch(self, inv: ToolInvocation) -> ToolResult:
        limit = min(int(inv.payload.get("limit", DEFAULT_ROW_LIMIT)), DEFAULT_ROW_LIMIT)
        owner, status = inv.payload.get("owner"), inv.payload.get("status")
        rows = [t for t in self.tasks.repo.list(lambda t: t.organization_id == inv.organization_id
                                                and (owner is None or t.owner == owner)
                                                and (status is None or t.status.value == status))]
        rows.sort(key=lambda t: t.created_at, reverse=True)
        data = [{"task_id": t.task_id, "title": t.title, "owner": t.owner, "priority": t.priority.value,
                 "status": t.status.value, "due_date": t.due_date.isoformat() if t.due_date else None}
                for t in rows[:limit]]
        return rows_result(inv, data, [f"task:{t.task_id}" for t in rows[:limit]], freshness=None, max_age=None,
                           limit=limit, now=self.clock())


@dataclass
class KnowledgeReadConnector:
    retriever: Any                              # knowledge.Retriever
    scopes_for_agent: Any                       # callable(agent_id) -> knowledge scopes
    clock: Clock = utc_now

    def fetch(self, inv: ToolInvocation) -> ToolResult:
        query = str(inv.payload.get("query", "")).strip()
        if not query:
            return ToolResult(False, error="query is required", status=ToolStatus.ERROR, tool_name=inv.tool_name)
        if inv.principal is None:
            return unavailable(inv, "no principal on the invocation; knowledge permissions cannot be checked")
        limit = min(int(inv.payload.get("limit", 10)), 20)
        hits = self.retriever.retrieve(organization_id=inv.organization_id, query=query,
                                       scopes=self.scopes_for_agent(inv.agent_id), principal=inv.principal,
                                       limit=limit, now=self.clock())
        if not hits:
            return unavailable(inv, "no readable knowledge matched the query")
        data = [{"source_id": h.source.source_id, "title": h.source.title, "text": h.chunk.text} for h in hits]
        return rows_result(inv, data, [h.source.source_id for h in hits], freshness=None, max_age=None, limit=limit,
                           now=self.clock(), extra_limitations=("keyword retrieval; not semantic search",))


# ---------------------------------------------------------------------------------------
# privacy-gated people summary (Part 36) - disabled by default
# ---------------------------------------------------------------------------------------
PEOPLE_BUSINESS_FIELDS = frozenset({"employee_id", "role", "department", "workload", "assigned_tasks",
                                    "goal_completion", "skills", "availability"})
PEOPLE_FORBIDDEN_FIELDS = frozenset({
    "health", "medical", "disability", "religion", "political_beliefs", "politics", "race", "ethnicity",
    "sexual_orientation", "gender_identity", "private_communications", "messages", "family", "marital_status",
    "pregnancy", "union_membership", "biometrics", "genetic", "criminal_record", "home_address", "date_of_birth",
    "age", "nationality", "immigration_status", "salary", "compensation", "bank_account", "national_id",
})


@dataclass
class PeopleSummaryConnector:
    """Returns only fields that are BOTH business-relevant and approved by the privacy review.
    Forbidden categories are stripped even if a review lists them."""

    row_source: Any                             # callable(organization_id) -> list[dict]
    allowed_fields: frozenset[str]
    clock: Clock = utc_now

    def __post_init__(self) -> None:
        self.allowed_fields = frozenset(self.allowed_fields) & PEOPLE_BUSINESS_FIELDS - PEOPLE_FORBIDDEN_FIELDS

    def fetch(self, inv: ToolInvocation) -> ToolResult:
        if not self.allowed_fields:
            return unavailable(inv, "privacy review approved no fields")
        rows = self.row_source(inv.organization_id)
        data = [{k: v for k, v in r.items() if k in self.allowed_fields} for r in rows[:DEFAULT_ROW_LIMIT]]
        return rows_result(inv, data, [f"people:{r.get('employee_id')}" for r in rows[:DEFAULT_ROW_LIMIT]],
                           freshness=None, max_age=None, limit=DEFAULT_ROW_LIMIT, now=self.clock(),
                           extra_limitations=("privacy-gated summary; sensitive personal data excluded",))
