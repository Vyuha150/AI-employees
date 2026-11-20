"""Escalation engine (Part 40).

Evaluates the organization's typed escalation rules (organization_policy.escalations) against
current state and creates, per triggered condition:
  an alert (always), a task (when the rule says create_task), a notification to the escalation
  target, and an `escalation.triggered` audit event.

Each (rule, object, period) escalates once (idempotency key). The engine NEVER executes a
corrective action: it informs and assigns; humans and agents act through the normal gateway.

Rules needing business data (KPI_THRESHOLD, DEAL_STALLED) use only sourced inputs: KPI
snapshots, or `signals` supplied by a read tool. Without data they are reported as not
evaluable; they never assume a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .alerts import Alert, AlertService, Severity
from .approvals import ApprovalEngine
from .audit import AuditLogger, EventType
from .common import ActorType, Clock, utc_now
from .idempotency import IdempotencyStore, InMemoryIdempotencyStore, idempotency_key
from .kpis import KpiService
from .policy import EscalationRuleSpec, OrganizationPolicy
from .state_machines import AlertStatus, ApprovalStatus, TaskStatus
from .tasks import Priority, TaskService

_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


@dataclass(frozen=True)
class Escalation:
    rule_id: str
    object_type: str
    object_id: str
    alert: Alert
    task_id: str | None
    notification_id: str


@dataclass(frozen=True)
class EscalationReport:
    triggered: tuple[Escalation, ...]
    not_evaluable: tuple[str, ...]


@dataclass
class EscalationEngine:
    policy: OrganizationPolicy
    audit: AuditLogger
    alerts: AlertService
    tasks: TaskService
    approvals: ApprovalEngine
    kpis: KpiService
    idempotency: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    clock: Clock = utc_now
    suppressed: int = 0

    # -- helpers ------------------------------------------------------------------------
    def _fire(self, rule: EscalationRuleSpec, *, object_type: str, object_id: str, title: str, description: str,
              period: str = "once", source_ids: Iterable[str] = ()) -> Escalation | None:
        org = self.policy.organization_id
        # One open alert per unresolved condition: while an alert for (rule, object) is OPEN or
        # ACKNOWLEDGED, re-evaluation does not create another, whatever the period.
        condition = f"escalation:{rule.rule_id}:{object_type}:{object_id}"
        if any(a.dedupe_key == condition and a.organization_id == org for a in self.alerts.repo.query(
                {"status__in": [AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED]}, limit=500)):
            self.suppressed += 1
            return None
        key = idempotency_key(org, "escalation", rule.rule_id, object_type, object_id, period)
        fresh, _ = self.idempotency.claim(org, key)
        if not fresh:
            return None
        alert = self.alerts.create(organization_id=org, severity=Severity(rule.severity), category=f"ESCALATION:{rule.type}",
                                   title=title, description=description, source_ids=source_ids,
                                   related_object_type=object_type, related_object_id=object_id, dedupe_key=condition)
        task_id = None
        if rule.create_task:
            human = rule.escalate_to.startswith("human:")
            task = self.tasks.create_from_contract({
                "title": f"Escalation: {title}"[:200], "objective": "Resolve the escalated condition",
                "description": description, "owner": rule.escalate_to,
                "priority": "P0" if rule.severity == "CRITICAL" else "P1", "dependencies": [],
                "expected_output": "Condition resolved or a documented decision",
                "completion_criteria": [f"Alert {alert.alert_id} resolved with a note"],
                "kpi_affected": [], "risk_level": "AMBER"},
                organization_id=org, created_by="escalation_engine", actor_type=ActorType.SYSTEM,
                owner_type=ActorType.HUMAN if human else ActorType.AGENT)
            task_id = task.task_id
        note = self.alerts.notify(organization_id=org, recipient=rule.escalate_to, subject=f"[{rule.severity}] {title}",
                                  body=description, alert_id=alert.alert_id)
        self.audit.record(EventType.ESCALATION_TRIGGERED, organization_id=org, actor_type=ActorType.SYSTEM,
                          actor_id="escalation_engine", object_type=object_type, object_id=object_id,
                          payload={"rule_id": rule.rule_id, "type": rule.type, "alert_id": alert.alert_id,
                                   "task_id": task_id, "escalate_to": rule.escalate_to})
        return Escalation(rule.rule_id, object_type, object_id, alert, task_id, note.notification_id)

    def _events(self, event_type: EventType, since: datetime) -> list:
        return [e for e in self.audit.store.events(event_type=event_type, organization_id=self.policy.organization_id)
                if e.timestamp >= since]

    def on_execution_unknown(self, approval: Any, result: Any = None) -> Escalation | None:
        """Called by the gateway the moment an external outcome becomes unknown."""
        rule = next((r for r in self.policy.escalations if r.type == "EXECUTION_UNKNOWN"), None)
        if rule is None:   # no configured rule still never hides it
            rule = EscalationRuleSpec("execution_unknown_default", "EXECUTION_UNKNOWN", "CRITICAL", "human:FOUNDER",
                                      True, {})
        return self._fire(rule, object_type="approval", object_id=approval.approval_id,
                          title=f"Execution outcome unknown: {approval.tool_name} ({approval.risk_class.value})",
                          description="The external call's outcome could not be determined "
                                      f"({getattr(result, 'error', None) or 'no detail'}). Verify in the external system "
                                      "and reconcile the approval. Automatic retry is disabled.")

    # -- evaluation ---------------------------------------------------------------------
    def evaluate(self, *, signals: Iterable[Mapping[str, Any]] = ()) -> EscalationReport:
        now = self.clock()
        out: list[Escalation] = []
        not_evaluable: list[str] = []
        signals = list(signals)
        org = self.policy.organization_id

        for rule in self.policy.escalations:
            p = rule.params
            fired: list[Escalation | None] = []
            if rule.type == "TASK_OVERDUE":
                prios = tuple(Priority(x) for x in p.get("priorities", ["P0", "P1"]))
                for t in self.tasks.overdue(now.date(), prios):
                    if t.organization_id == org:
                        fired.append(self._fire(rule, object_type="task", object_id=t.task_id,
                                                title=f"Overdue {t.priority.value} task: {t.title}",
                                                description=f"Due {t.due_date}, owner {t.owner}, status {t.status.value}."))
            elif rule.type == "TASK_BLOCKED_TOO_LONG":
                limit = timedelta(hours=float(p["hours"]))
                for t in self.tasks.repo.list(lambda t: t.status is TaskStatus.BLOCKED and t.organization_id == org):
                    entered = [e.timestamp for e in self.audit.store.events(object_type="task", object_id=t.task_id)
                               if e.new_state == TaskStatus.BLOCKED.value]
                    if entered and now - max(entered) >= limit:
                        fired.append(self._fire(rule, object_type="task", object_id=t.task_id,
                                                period=max(entered).isoformat(),
                                                title=f"Task blocked > {p['hours']}h: {t.title}",
                                                description=f"Blocked reason: {t.blocked_reason}. Owner {t.owner}."))
            elif rule.type == "APPROVAL_PENDING_TOO_LONG":
                limit = timedelta(hours=float(p["hours"]))
                for a in self.approvals.repo.list(lambda a: a.status is ApprovalStatus.PENDING and a.organization_id == org):
                    if now - a.requested_at >= limit:
                        fired.append(self._fire(rule, object_type="approval", object_id=a.approval_id,
                                                title=f"Approval pending > {p['hours']}h: {a.tool_name}",
                                                description=f"{a.risk_class.value} approval requested by {a.agent_id}; "
                                                            f"needs role {a.required_role}; expires {a.expires_at.isoformat()}."))
            elif rule.type in ("TOOL_FAILURE_REPEATED", "RUN_FAILURE_REPEATED"):
                window = now - timedelta(hours=float(p["window_hours"]))
                et = EventType.TOOL_FAILED if rule.type == "TOOL_FAILURE_REPEATED" else EventType.AGENT_RUN_FAILED
                counts: dict[tuple[str, str], int] = {}
                for e in self._events(et, window):
                    k = (e.agent_id or "?", e.object_id if et is EventType.TOOL_FAILED else "run")
                    counts[k] = counts.get(k, 0) + 1
                for (agent, obj), n in counts.items():
                    if n >= int(p["count"]):
                        what = f"tool {obj}" if et is EventType.TOOL_FAILED else "runs"
                        fired.append(self._fire(rule, object_type="agent", object_id=f"{agent}:{obj}",
                                                period=now.date().isoformat(),
                                                title=f"{agent}: {n} failures of {what} in {p['window_hours']}h",
                                                description="Repeated failures; check connectors and recent changes."))
            elif rule.type == "EXECUTION_UNKNOWN":
                for a in self.approvals.repo.list(lambda a: a.status is ApprovalStatus.EXECUTION_UNKNOWN
                                                  and a.organization_id == org):
                    fired.append(self._fire(rule, object_type="approval", object_id=a.approval_id,
                                            title=f"Execution outcome unknown: {a.tool_name} ({a.risk_class.value})",
                                            description="The external call's outcome could not be determined. Verify in "
                                                        "the external system and reconcile the approval. Do not retry."))
            elif rule.type == "KPI_THRESHOLD":
                snap = self.kpis.latest(org, p["kpi"])
                if snap is None:
                    not_evaluable.append(f"{rule.rule_id}: no sourced observation for KPI {p['kpi']!r}")
                    continue
                if _OPS[p["operator"]](snap.value, Decimal(str(p["threshold"]))):
                    fired.append(self._fire(rule, object_type="kpi_snapshot", object_id=snap.snapshot_id,
                                            title=f"{p['kpi']} {p['operator']} {p['threshold']} (observed {snap.value} {snap.unit})",
                                            description=f"Observed at {snap.observed_at}; source {snap.source_reference}.",
                                            source_ids=(snap.source_reference,)))
            elif rule.type == "CRITICAL_RISK_DETECTED":
                for a in self.alerts.list(org, status=[AlertStatus.OPEN]):
                    if a.severity is Severity.CRITICAL and a.category.upper().startswith("RISK"):
                        fired.append(self._fire(rule, object_type="alert", object_id=a.alert_id,
                                                title=f"Critical risk: {a.title}", description=a.description,
                                                source_ids=a.source_ids))
            elif rule.type == "CUSTOMER_HEALTH_LOW":
                custs = [s for s in signals if s.get("type") == "customer"]
                if not custs:
                    not_evaluable.append(f"{rule.rule_id}: no customer data supplied by a connector")
                    continue
                for c in custs:
                    if not c.get("source_id") or c.get("health_score") is None:
                        continue
                    if Decimal(str(c["health_score"])) < Decimal(str(p["threshold"])):
                        fired.append(self._fire(rule, object_type="customer", object_id=str(c["id"]),
                                                title=f"Customer health {c['health_score']} below {p['threshold']}: "
                                                      f"{c.get('name', c['id'])}",
                                                description="Critical customer-health deterioration (sourced).",
                                                source_ids=(c["source_id"],)))
            elif rule.type == "PROJECT_BLOCKED":
                projects = [s for s in signals if s.get("type") == "project"]
                if not projects:
                    not_evaluable.append(f"{rule.rule_id}: no project data supplied by a connector")
                    continue
                for pr in projects:
                    if not pr.get("source_id") or pr.get("status") != "BLOCKED" or pr.get("blocked_days") is None:
                        continue
                    if int(pr["blocked_days"]) >= int(p["days"]):
                        fired.append(self._fire(rule, object_type="project", object_id=str(pr["id"]),
                                                title=f"Project blocked {pr['blocked_days']} days: {pr.get('name', pr['id'])}",
                                                description="Blocked project needs a decision or unblocking owner.",
                                                source_ids=(pr["source_id"],)))
            elif rule.type == "COMPLIANCE_DEADLINE":
                horizon = now.date() + timedelta(days=int(p["days"]))
                for t in self.tasks.repo.list(lambda t: t.organization_id == org and t.due_date is not None
                                              and t.due_date <= horizon and "missed_compliance_events" in t.kpi_affected
                                              and t.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)):
                    fired.append(self._fire(rule, object_type="task", object_id=t.task_id,
                                            title=f"Compliance deadline {t.due_date}: {t.title}",
                                            description=f"Owner {t.owner}; status {t.status.value}."))
            elif rule.type == "DEAL_STALLED":
                deals = [s for s in signals if s.get("type") == "opportunity"]
                if not deals:
                    not_evaluable.append(f"{rule.rule_id}: no opportunity data supplied by a read tool")
                    continue
                for d in deals:
                    if not d.get("source_id"):
                        not_evaluable.append(f"{rule.rule_id}: opportunity {d.get('id')} has no source_id")
                        continue
                    if Decimal(str(d["value"])) >= Decimal(str(p["min_value"])) and int(d["days_since_activity"]) >= int(p["days"]):
                        fired.append(self._fire(rule, object_type="opportunity", object_id=str(d["id"]),
                                                period=now.date().isoformat(),
                                                title=f"High-value deal stalled {d['days_since_activity']} days: {d.get('name', d['id'])}",
                                                description=f"Value {d['value']}; no activity for {d['days_since_activity']} days.",
                                                source_ids=(d["source_id"],)))
            out.extend(e for e in fired if e is not None)
        return EscalationReport(tuple(out), tuple(not_evaluable))


# ---------------------------------------------------------------------------------------
# signals from connectors (sourced records only)
# ---------------------------------------------------------------------------------------
def _days_since(value: Any, now: datetime) -> int | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    return (now - ts).days


def collect_signals(read_tools: Any, organization_id: str, now: datetime) -> tuple[list[dict], list[str]]:
    """Read connector views for opportunities, customers and projects. Records without a source id
    are ignored; absent connectors are reported, never filled in."""
    from .executors import ToolInvocation
    signals: list[dict] = []
    notes: list[str] = []
    if read_tools is None:
        return signals, ["no read tools configured"]
    for tool, kind in (("read_opportunities", "opportunity"), ("read_customers", "customer"),
                       ("read_projects", "project")):
        if not read_tools.supports(tool):
            notes.append(f"{tool}: no connector configured")
            continue
        res = read_tools.execute(ToolInvocation(organization_id, "escalation_engine", "-", tool, tool, {"limit": 50}))
        if not res.success:
            notes.append(f"{tool}: {res.status.value} {res.error or ''}".strip())
            continue
        for rec, sid in zip(res.data.get("records", []), res.source_ids):
            item = {"type": kind, "id": rec.get("source_record_id") or rec.get("record_id"), "name": rec.get("name"),
                    "source_id": sid}
            if kind == "opportunity":
                item.update(value=rec.get("amount"), days_since_activity=_days_since(rec.get("last_activity_at"), now))
                if item["value"] is None or item["days_since_activity"] is None:
                    continue
            elif kind == "customer":
                item["health_score"] = rec.get("health_score")
            else:
                item.update(status=rec.get("status"), blocked_days=_days_since(rec.get("blocked_since"), now))
            signals.append(item)
    return signals, notes
