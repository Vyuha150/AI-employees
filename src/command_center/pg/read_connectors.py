"""PostgreSQL-backed read-tool connectors (Part 34).

read_kpis, read_tasks, read_knowledge, search_company_data read Command Center tables.
read_customers, read_leads, read_opportunities, read_projects, read_vendors and
read_financial_summary read ORGANIZATION-PROVIDED views in the `connector` schema:

    connector.customers, connector.leads, connector.opportunities, connector.projects,
    connector.vendors, connector.financial_summary

Each view must expose organization_id uuid, record_id text and updated_at timestamptz plus
any business columns. If the view does not exist the tool returns UNAVAILABLE; nothing is
synthesized. Rows older than the connector's max_age make the result STALE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg

from ..common import Clock, utc_now
from ..executors import ToolInvocation, ToolResult, ToolStatus
from ..read_tools import DEFAULT_ROW_LIMIT, rows_result, unavailable
from .database import Database

VIEW_TOOLS = {"read_customers": "customers", "read_leads": "leads", "read_opportunities": "opportunities",
              "read_projects": "projects", "read_vendors": "vendors", "read_financial_summary": "financial_summary"}


@dataclass
class ViewReadConnector:
    db: Database
    org_uuid: str
    view: str
    max_age: timedelta | None = timedelta(days=2)
    clock: Clock = utc_now

    def fetch(self, inv: ToolInvocation) -> ToolResult:
        limit = min(int(inv.payload.get("limit", DEFAULT_ROW_LIMIT)), DEFAULT_ROW_LIMIT)
        with self.db.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"connector.{self.view}",))
            if cur.fetchone()[0] is None:
                return unavailable(inv, f"connector view connector.{self.view} is not configured; data unavailable")
            try:
                cur.execute(f"SELECT to_jsonb(v), v.record_id, v.updated_at FROM connector.{self.view} v "
                            "WHERE v.organization_id = %s ORDER BY v.updated_at DESC LIMIT %s",
                            (self.org_uuid, limit))
            except psycopg.Error as exc:
                return ToolResult(False, error=f"connector view error: {exc.sqlstate}", status=ToolStatus.ERROR,
                                  tool_name=inv.tool_name)
            rows = cur.fetchall()
        if not rows:
            return unavailable(inv, f"connector.{self.view} returned no rows for this organization")
        data = [{k: v for k, v in r[0].items() if k != "organization_id"} for r in rows]
        newest = max(r[2] for r in rows if r[2] is not None) if any(r[2] for r in rows) else None
        return rows_result(inv, data, [f"db:{self.view}:{r[1]}" for r in rows], freshness=newest,
                           max_age=self.max_age, limit=limit, now=self.clock())


@dataclass
class PgKnowledgeSearchConnector:
    """Keyword search over knowledge_chunks, filtered by document permissions and agent scopes."""

    db: Database
    org_uuid: str
    scopes_for_agent: Any
    clock: Clock = utc_now

    def fetch(self, inv: ToolInvocation) -> ToolResult:
        query = str(inv.payload.get("query", "")).strip()
        if not query or inv.principal is None:
            return ToolResult(False, error="query and principal are required", status=ToolStatus.ERROR,
                              tool_name=inv.tool_name)
        roles = sorted(inv.principal.roles) + ["ALL"]
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.text, d.title, c.ordinal FROM knowledge_chunks c JOIN knowledge_documents d "
                "ON d.organization_id = c.organization_id AND d.id = c.document_id "
                "WHERE c.organization_id = %s AND d.category = ANY(%s) AND d.permissions && %s "
                "AND (d.valid_from IS NULL OR d.valid_from <= now()) AND (d.valid_until IS NULL OR d.valid_until > now()) "
                "AND c.text ILIKE %s ORDER BY d.created_at DESC, c.ordinal LIMIT 20",
                (self.org_uuid, list(self.scopes_for_agent(inv.agent_id)), roles, f"%{query}%"))
            rows = cur.fetchall()
        if not rows:
            return unavailable(inv, "no readable knowledge matched the query")
        data = [{"source_id": f"kb:{r[0]}", "title": r[2], "text": r[1]} for r in rows]
        return rows_result(inv, data, [f"kb:{r[0]}" for r in rows], freshness=None, max_age=None, limit=20,
                           now=self.clock(), extra_limitations=("substring search; not semantic",))


def pg_read_connectors(db: Database, org_uuid: str, scopes_for_agent: Any, clock: Clock = utc_now) -> dict[str, Any]:
    out: dict[str, Any] = {tool: ViewReadConnector(db, org_uuid, view, clock=clock) for tool, view in VIEW_TOOLS.items()}
    out["read_knowledge"] = PgKnowledgeSearchConnector(db, org_uuid, scopes_for_agent, clock)
    out["search_company_data"] = PgKnowledgeSearchConnector(db, org_uuid, scopes_for_agent, clock)
    return out
