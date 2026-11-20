"""Connector read-view contracts (Part 6 of the Phase 4 brief).

Organizations own the views in the `connector` schema. Each view must expose the common
columns below plus its business columns. The Command Center reads them through read tools; it
never writes them and never substitutes data when a view is absent (UNAVAILABLE).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

COMMON_COLUMNS = ("organization_id", "source_system", "source_record_id", "source_updated_at")
LEGACY_COLUMNS = ("organization_id", "record_id", "updated_at")       # Phase 3 shape, still accepted


@dataclass(frozen=True)
class ConnectorContract:
    view: str
    tool: str
    business_columns: tuple[str, ...]
    max_age: timedelta
    description: str

    @property
    def required_columns(self) -> tuple[str, ...]:
        return COMMON_COLUMNS + self.business_columns


CONTRACTS: dict[str, ConnectorContract] = {c.view: c for c in (
    ConnectorContract("customers", "read_customers", ("name", "status", "health_score"), timedelta(days=2),
                      "Customer accounts; health_score 0-100 (NULL when not measured)"),
    ConnectorContract("leads", "read_leads", ("name", "company", "stage", "score"), timedelta(days=2),
                      "Leads and prospects with qualification stage and score"),
    ConnectorContract("opportunities", "read_opportunities",
                      ("name", "customer_id", "stage", "amount", "currency", "last_activity_at"), timedelta(days=1),
                      "Open and closed opportunities; amount in currency"),
    ConnectorContract("projects", "read_projects", ("name", "status", "due_date", "blocked_since"),
                      timedelta(days=1), "Delivery projects; status BLOCKED with blocked_since when blocked"),
    ConnectorContract("vendors", "read_vendors", ("name", "status", "lead_time_days"), timedelta(days=7),
                      "Suppliers with current status and lead time"),
    ConnectorContract("financial_summary", "read_financial_summary", ("period", "metric", "value", "currency"),
                      timedelta(days=2), "Financial metrics by period (cash, AR, AP, revenue, margin, ...)"),
)}
TOOL_TO_VIEW = {c.tool: c.view for c in CONTRACTS.values()}


def contract_sql_example(view: str) -> str:
    """Documentation helper: a view definition skeleton satisfying the contract."""
    c = CONTRACTS[view]
    cols = ",\n  ".join(f"<source expression> AS {col}" for col in c.required_columns)
    return f"CREATE VIEW connector.{view} AS SELECT\n  {cols}\nFROM <your source table>;"
