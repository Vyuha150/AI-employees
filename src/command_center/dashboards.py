"""Dashboard read models for the frontend (one request per dashboard).

Everything is assembled from existing records: KPI snapshots (sourced), the latest valid daily
brief, alerts, approvals, runs, outputs, usage and performance. Missing data is returned as null
with a freshness note; nothing is estimated or filled in.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from .common import Principal
from .errors import NotFoundError
from .performance import Period, compute_agent_performance
from .state_machines import AlertStatus, ApprovalStatus, RunStatus

# Domain -> KPI keys (definitions live in agent profiles)
EXECUTIVE_KPIS = {
    "revenue": ("revenue", "sales_gross_profit", "gross_margin"),
    "cash": ("cash_runway", "working_capital", "ar_days"),
    "pipeline": ("weighted_pipeline", "win_rate", "qualified_leads"),
    "projects": ("on_time_delivery", "sla_compliance", "project_margin"),
    "customers": ("retention", "churn", "customer_health", "renewal_rate"),
}
STALE_AFTER = timedelta(days=7)


def _kpi_block(cc: Any, org: str, keys: tuple[str, ...]) -> dict:
    now = cc.clock()
    items, stale, missing = [], [], []
    for k in keys:
        snap = cc.kpis.latest(org, k)
        if snap is None:
            missing.append(k)
            items.append({"kpi": k, "value": None, "unit": None, "observed_at": None, "source_reference": None})
            continue
        target = cc.policy.kpi_targets.get(k, {}).get("target")
        if now - snap.recorded_at > STALE_AFTER:
            stale.append(k)
        items.append({"kpi": k, "value": str(snap.value), "unit": snap.unit, "observed_at": snap.observed_at,
                      "source_reference": snap.source_reference, "target": None if target is None else str(target),
                      "variance": None if target is None else str(snap.value - Decimal(target))})
    status = "UNAVAILABLE" if len(missing) == len(keys) else ("PARTIAL" if missing or stale else "OK")
    return {"status": status, "kpis": items, "missing": missing, "stale": stale}


def _latest_brief(cc: Any, org: str) -> Any:
    rows = cc.outputs.repo.query({"kind": "DAILY_BRIEF", "validity": "VALID"}, order_by="-created_at", limit=1)
    rows = [o for o in rows if o.organization_id == org]
    return rows[0] if rows else None


def executive_dashboard(api: Any, p: Principal) -> dict:
    cc = api._cc(p)
    org = p.organization_id
    brief = _latest_brief(cc, org)
    content = brief.content if brief else None
    open_alerts = cc.alerts.repo.query({"status__in": [AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED]},
                                       order_by="-created_at", limit=200)
    open_alerts = [a for a in open_alerts if a.organization_id == org]
    pending = [a for a in cc.approvals.repo.query({"status": ApprovalStatus.PENDING}, order_by="requested_at",
                                                  limit=200) if a.organization_id == org]
    unknown = [a for a in cc.approvals.repo.query({"status": ApprovalStatus.EXECUTION_UNKNOWN}, limit=200)
               if a.organization_id == org]
    domains = {d: _kpi_block(cc, org, keys) for d, keys in EXECUTIVE_KPIS.items()}
    freshness = {
        "latest_brief": None if brief is None else {"output_id": brief.output_id,
                                                    "brief_date": content.get("brief_date"),
                                                    "created_at": brief.created_at.isoformat(),
                                                    "data_quality": content.get("data_quality")},
        "kpi_domains": {d: {"status": b["status"], "missing": b["missing"], "stale": b["stale"]}
                        for d, b in domains.items()},
        "connectors": [{"view": c["view"], "state": c["state"]} for c in api.connector_status(p)],
    }
    return {
        "business_health": content.get("business_health") if content else None,
        **domains,
        "risks": [{"alert_id": a.alert_id, "severity": a.severity.value, "title": a.title, "category": a.category,
                   "created_at": a.created_at.isoformat()} for a in open_alerts
                  if a.severity.value in ("CRITICAL", "WARNING")][:20],
        "pending_approvals": {"count": len(pending), "outcome_unknown": len(unknown),
                              "items": [{"approval_id": a.approval_id, "tool_name": a.tool_name,
                                         "risk_class": a.risk_class.value, "agent_id": a.agent_id,
                                         "required_role": a.required_role, "expires_at": a.expires_at.isoformat()}
                                        for a in pending[:10]]},
        "priorities": content.get("todays_priorities", []) if content else [],
        "alerts": {"open": len(open_alerts),
                   "by_severity": {s: sum(1 for a in open_alerts if a.severity.value == s)
                                   for s in ("CRITICAL", "WARNING", "WATCH", "INFO")}},
        "data_freshness": freshness,
    }


def agent_dashboard(api: Any, p: Principal, agent_id: str) -> dict:
    cc = api._cc(p)
    org = p.organization_id
    try:
        agent = cc.registry.get_profile(agent_id)
    except Exception:
        raise NotFoundError("agent not found") from None
    runs = [r for r in cc.runs.repo.query({"agent_id": agent_id}, order_by="-created_at", limit=20)
            if r.organization_id == org]
    active = next((r for r in runs if r.status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.TOOL_WAIT,
                                                   RunStatus.APPROVAL_REQUIRED)), None)
    outputs = [o for o in cc.outputs.repo.query({"agent_id": agent_id}, order_by="-created_at", limit=5)
               if o.organization_id == org]
    pending = [a for a in cc.approvals.repo.query({"agent_id": agent_id, "status": ApprovalStatus.PENDING},
                                                  limit=50) if a.organization_id == org]
    alerts = [a for a in cc.alerts.repo.query({"status__in": [AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED]},
                                              order_by="-created_at", limit=200)
              if a.organization_id == org and a.source_agent == agent_id][:20]
    tasks = [t for t in cc.tasks.repo.query({"owner": agent_id}, order_by="created_at", limit=100)
             if t.organization_id == org and t.status.value in ("QUEUED", "IN_PROGRESS", "WAITING_HUMAN",
                                                                  "WAITING_AGENT", "BLOCKED")]
    since = cc.clock() - timedelta(days=30)
    usage = cc.usage.repo.list(lambda u: u.agent_id == agent_id and u.timestamp >= since
                               and u.organization_id == org)
    known = [u.estimated_cost for u in usage if u.estimated_cost is not None]
    perf = compute_agent_performance(cc.audit.store, agent_id, Period.D30, cc.clock())
    return {
        "agent": {"agent_id": agent.agent_id, "name": agent.name, "role": agent.role.value,
                  "department": agent.department, "enabled": agent.enabled, "reports_to": agent.reports_to,
                  "autonomy_level": agent.autonomy_level.value, "model_profile": agent.model_profile},
        "status": "DISABLED" if not agent.enabled else ("ACTIVE" if active else "IDLE"),
        "active_run": None if active is None else {"run_id": active.run_id, "status": active.status.value,
                                                   "objective": active.objective,
                                                   "created_at": active.created_at.isoformat()},
        "active_tasks": [{"task_id": t.task_id, "title": t.title, "status": t.status.value, "priority": t.priority.value,
                          "due_date": t.due_date.isoformat() if t.due_date else None} for t in tasks[:10]],
        "kpis": _kpi_block(cc, org, tuple(k.name for k in agent.kpis)),
        "recent_outputs": [api._output(o, content=False) for o in outputs],
        "performance_30d": {k: (str(v) if hasattr(v, "quantize") else v)
                            for k, v in perf.__dict__.items() if k not in ("period_start", "period_end", "period")},
        "pending_approvals": [{"approval_id": a.approval_id, "tool_name": a.tool_name, "risk_class": a.risk_class.value,
                               "expires_at": a.expires_at.isoformat()} for a in pending],
        "alerts": [{"alert_id": a.alert_id, "severity": a.severity.value, "title": a.title} for a in alerts],
        "usage_30d": {"records": len(usage), "input_tokens": sum(u.input_tokens for u in usage),
                      "output_tokens": sum(u.output_tokens for u in usage),
                      "known_cost": str(sum(known, Decimal(0))),
                      "unpriced_records": sum(1 for u in usage if u.estimated_cost is None),
                      "currency": next((u.currency for u in usage if u.currency), None)},
    }
