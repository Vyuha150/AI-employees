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

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import psycopg

from ..common import Clock, utc_now
from ..connectors import COMMON_COLUMNS, CONTRACTS, LEGACY_COLUMNS
from ..executors import ToolInvocation, ToolResult, ToolStatus
from ..read_tools import DEFAULT_ROW_LIMIT, rows_result, unavailable
from .database import Database

VIEW_TOOLS = {"read_customers": "customers", "read_leads": "leads", "read_opportunities": "opportunities",
              "read_projects": "projects", "read_vendors": "vendors", "read_financial_summary": "financial_summary"}


def view_columns(cur, view: str) -> set[str]:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'connector' "
                "AND table_name = %s", (view,))
    return {r[0] for r in cur.fetchall()}


def record_status(db: Database, org_uuid: str, view: str, *, error: str | None) -> None:
    with db.cursor() as cur:
        if error is None:
            cur.execute("INSERT INTO connector_status (organization_id, view_name, last_success_at) VALUES (%s, %s, now()) "
                        "ON CONFLICT (organization_id, view_name) DO UPDATE SET last_success_at = now(), "
                        "updated_at = now()", (org_uuid, view))
        else:
            cur.execute("INSERT INTO connector_status (organization_id, view_name, last_error_at, last_error) "
                        "VALUES (%s, %s, now(), %s) ON CONFLICT (organization_id, view_name) DO UPDATE SET "
                        "last_error_at = now(), last_error = EXCLUDED.last_error, updated_at = now()",
                        (org_uuid, view, error[:500]))


@dataclass
class ViewReadConnector:
    """Reads one organization-owned connector view under its contract (connectors.CONTRACTS)."""

    db: Database
    org_uuid: str
    view: str
    max_age: timedelta | None = None
    clock: Clock = utc_now

    def __post_init__(self) -> None:
        if self.max_age is None:
            self.max_age = CONTRACTS[self.view].max_age if self.view in CONTRACTS else timedelta(days=2)

    def fetch(self, inv: ToolInvocation) -> ToolResult:
        limit = min(int(inv.payload.get("limit", DEFAULT_ROW_LIMIT)), DEFAULT_ROW_LIMIT)
        contract = CONTRACTS.get(self.view)
        warnings: list[str] = []
        with self.db.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"connector.{self.view}",))
            if cur.fetchone()[0] is None:
                return unavailable(inv, f"connector view connector.{self.view} is not configured; data unavailable")
            cols = view_columns(cur, self.view)
            if set(COMMON_COLUMNS) <= cols:
                id_col, ts_col, sys_col = "source_record_id", "source_updated_at", "source_system"
                missing = [c for c in (contract.business_columns if contract else ()) if c not in cols]
                if missing:
                    warnings.append(f"view lacks contract columns {missing}")
            elif set(LEGACY_COLUMNS) <= cols:
                id_col, ts_col, sys_col = "record_id", "updated_at", None
                warnings.append("legacy connector view (record_id/updated_at); add source_system, source_record_id, "
                                "source_updated_at to meet the contract")
            else:
                missing = [c for c in COMMON_COLUMNS if c not in cols]
                err = f"connector.{self.view} does not satisfy the contract; missing {missing}"
                record_status(self.db, self.org_uuid, self.view, error=err)
                return ToolResult(False, error=err, status=ToolStatus.ERROR, tool_name=inv.tool_name)
            try:
                cur.execute(f"SELECT to_jsonb(v), v.{id_col}::text, v.{ts_col}"
                            f"{', v.' + sys_col if sys_col else ', NULL'} FROM connector.{self.view} v "
                            f"WHERE v.organization_id = %s ORDER BY v.{ts_col} DESC NULLS LAST LIMIT %s",
                            (self.org_uuid, limit))
                rows = cur.fetchall()
            except psycopg.Error as exc:
                err = f"connector view error: {exc.sqlstate}"
                record_status(self.db, self.org_uuid, self.view, error=err)
                return ToolResult(False, error=err, status=ToolStatus.ERROR, tool_name=inv.tool_name)
        if not rows:
            return unavailable(inv, f"connector.{self.view} returned no rows for this organization")
        retrieved = self.clock().isoformat()
        data = []
        for rec, rid, ts, system in rows:
            item = {k: v for k, v in rec.items() if k != "organization_id"}
            item.update({"organization_id": inv.organization_id, "source_system": system or "unspecified",
                         "source_record_id": rid, "source_updated_at": ts.isoformat() if ts else None,
                         "retrieved_at": retrieved})
            data.append(item)
        newest = max((r[2] for r in rows if r[2] is not None), default=None)
        record_status(self.db, self.org_uuid, self.view, error=None)
        result = rows_result(inv, data, [f"db:{self.view}:{r[1]}" for r in rows], freshness=newest,
                             max_age=self.max_age, limit=limit, now=self.clock())
        return replace(result, warnings=tuple(warnings)) if warnings else result


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
