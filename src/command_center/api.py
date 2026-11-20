"""API-facing service layer (Part 28). Framework-neutral: a REST or RPC layer authenticates the
caller, builds a Principal and calls these methods. Results are plain JSON-ready dicts.

Authorization
- Every call resolves the tenant from the principal (TenantRegistry); records of another
  organization are NotFound.
- Reads require organization membership. Writes require explicit permissions:
    tasks:write  runs:cancel (or being the run's initiator)  outputs:review
    recommendations:decide  changes:write  audit:read  performance:read
  Action approvals are role-checked by the ApprovalEngine (policy approver roles).
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from .briefing import BriefingService
from .commands import CommandRequest, CommandService
from .common import ActorType, Principal
from .errors import NotFoundError, PermissionDeniedError
from .evaluation import OutputEvaluation, ReviewDecision
from .performance import Period, compute_agent_performance
from .state_machines import RecommendationStatus, RunStatus, TaskStatus
from .tenancy import TenantRegistry, owned


def to_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: to_json(v) for k, v in asdict(value).items() if k not in ("principal",)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json(v) for v in value]
    return value


def _q(repo: Any, p: Principal, filters: Mapping[str, Any], order_by: str, limit: int, offset: int) -> list[Any]:
    """Database-side filtered, ordered, paginated listing scoped to the principal's organization."""
    flt = {k: v for k, v in filters.items() if v is not None}
    rows = repo.query(flt, order_by=order_by, limit=limit, offset=offset)
    return [r for r in rows if getattr(r, "organization_id", None) == p.organization_id]


def _need(principal: Principal, permission: str) -> None:
    if not principal.has(permission):
        raise PermissionDeniedError(f"missing permission {permission!r}")


class ApiService:
    def __init__(self, tenants: TenantRegistry, commands: CommandService | None = None):
        self.tenants = tenants
        self.commands = commands or CommandService(tenants)

    def _cc(self, p: Principal):
        return self.tenants.for_principal(p)

    # -- agents -----------------------------------------------------------------------
    def list_agents(self, p: Principal) -> list[dict]:
        return [self._agent(a) for a in self._cc(p).registry.list_agents(include_disabled=True)]

    def get_agent(self, p: Principal, agent_id: str) -> dict:
        try:
            return self._agent(self._cc(p).registry.get_profile(agent_id))
        except Exception:
            raise NotFoundError("agent not found") from None

    @staticmethod
    def _agent(a) -> dict:
        return {"agent_id": a.agent_id, "name": a.name, "role": a.role.value, "department": a.department,
                "reports_to": a.reports_to, "enabled": a.enabled, "mission": a.mission,
                "responsibilities": list(a.responsibilities), "kpis": [k.name for k in a.kpis],
                "allowed_tools": list(a.allowed_tools), "autonomy_level": a.autonomy_level.value,
                "model_profile": a.model_profile}

    # -- runs -------------------------------------------------------------------------
    def create_command(self, p: Principal, *, agent_id: str, message: str, attachments: tuple[str, ...] = (),
                       context_selection: tuple = (), conversation_id: str | None = None,
                       idempotency_key: str | None = None) -> dict:
        return to_json(self.commands.submit(p, CommandRequest(p.organization_id, agent_id, message, tuple(attachments),
                                                              tuple(context_selection), conversation_id,
                                                              idempotency_key)))

    def get_run(self, p: Principal, run_id: str) -> dict:
        return to_json(owned(self._cc(p).runs.repo.find(run_id), p, "run"))

    def list_runs(self, p: Principal, *, agent_id: str | None = None, status: str | None = None,
                  limit: int = 50, offset: int = 0) -> list[dict]:
        rows = _q(self._cc(p).runs.repo, p, {"agent_id": agent_id, "status": status}, "-created_at", limit, offset)
        return [to_json(r) for r in rows]

    def cancel_run(self, p: Principal, run_id: str, reason: str) -> dict:
        cc = self._cc(p)
        run = owned(cc.runs.repo.find(run_id), p, "run")
        if run.triggered_by != p.principal_id:
            _need(p, "runs:cancel")
        for a in cc.approvals.pending_for_run(run_id):
            cc.approvals.cancel(a.approval_id, ActorType.HUMAN, p.principal_id, f"run cancelled: {reason}")
        return to_json(cc.runs.transition(run_id, RunStatus.CANCELLED, reason=reason, actor_type=ActorType.HUMAN,
                                          actor_id=p.principal_id))

    # -- conversations ----------------------------------------------------------------
    def list_conversations(self, p: Principal, limit: int = 50) -> list[dict]:
        return [to_json(c) for c in self._cc(p).conversations.list_conversations(p, limit=limit)]

    def list_messages(self, p: Principal, conversation_id: str, *, after_seq: int = 0, limit: int = 50) -> dict:
        page = self._cc(p).conversations.list_messages(p, conversation_id, after_seq=after_seq, limit=limit)
        return {"items": [to_json(m) for m in page.items], "next_cursor": page.next_cursor}

    # -- tasks ------------------------------------------------------------------------
    def list_tasks(self, p: Principal, *, status: str | None = None, owner: str | None = None,
                   limit: int = 100, offset: int = 0) -> list[dict]:
        rows = _q(self._cc(p).tasks.repo, p, {"status": status, "owner": owner}, "created_at", limit, offset)
        return [to_json(t) for t in rows]

    def create_task(self, p: Principal, data: Mapping[str, Any]) -> dict:
        _need(p, "tasks:write")
        return to_json(self._cc(p).tasks.create_from_contract(data, organization_id=p.organization_id,
                                                              created_by=p.principal_id, actor_type=ActorType.HUMAN,
                                                              owner_type=ActorType(data.get("owner_type", "AGENT"))))

    def update_task(self, p: Principal, task_id: str, status: str, reason: str | None = None) -> dict:
        _need(p, "tasks:write")
        cc = self._cc(p)
        owned(cc.tasks.repo.find(task_id), p, "task")
        return to_json(cc.tasks.transition(task_id, TaskStatus(status), actor_type=ActorType.HUMAN,
                                           actor_id=p.principal_id, reason=reason))

    # -- outputs ----------------------------------------------------------------------
    def list_outputs(self, p: Principal, *, agent_id: str | None = None, kind: str | None = None,
                     limit: int = 50, offset: int = 0) -> list[dict]:
        rows = _q(self._cc(p).outputs.repo, p, {"agent_id": agent_id, "kind": kind}, "-created_at", limit, offset)
        return [self._output(o, content=False) for o in rows]

    def get_output(self, p: Principal, output_id: str) -> dict:
        return self._output(owned(self._cc(p).outputs.repo.find(output_id), p, "output"), content=True)

    @staticmethod
    def _output(o, content: bool) -> dict:
        d = {"output_id": o.output_id, "run_id": o.run_id, "agent_id": o.agent_id, "kind": o.kind.value,
             "response_type": o.response_type, "validity": o.validity.value, "errors": list(o.validation_errors),
             "warnings": list(o.validation_warnings), "created_at": o.created_at.isoformat(),
             "review_decision": o.review_decision.value if o.review_decision else None}
        if content:
            d["content"] = o.content
        return d

    def submit_output_evaluation(self, p: Principal, output_id: str, *, accuracy: int, relevance: int,
                                 completeness: int, actionability: int, business_value: int, decision: str,
                                 comment: str | None = None) -> dict:
        _need(p, "outputs:review")
        cc = self._cc(p)
        owned(cc.outputs.repo.find(output_id), p, "output")
        ev = OutputEvaluation(output_id, p.principal_id, cc.clock(), accuracy, relevance, completeness, actionability,
                              business_value, ReviewDecision(decision), comment)
        out = cc.outputs.review(ev, p)
        return {"output_id": out.output_id, "composite_score": str(ev.composite_score), "decision": ev.decision.value}

    # -- recommendations --------------------------------------------------------------
    def list_recommendations(self, p: Principal, *, status: str | None = None, limit: int = 100,
                             offset: int = 0) -> list[dict]:
        rows = _q(self._cc(p).recommendations.repo, p, {"status": status}, "created_at", limit, offset)
        return [to_json(r) for r in rows]

    def _decide_rec(self, p: Principal, rec_id: str, target: RecommendationStatus, reason: str | None) -> dict:
        _need(p, "recommendations:decide")
        cc = self._cc(p)
        rec = owned(cc.recommendations.repo.find(rec_id), p, "recommendation")
        if rec.status is RecommendationStatus.PROPOSED and target is RecommendationStatus.APPROVED:
            cc.recommendations.transition(rec_id, RecommendationStatus.UNDER_REVIEW, actor_type=ActorType.HUMAN,
                                          actor_id=p.principal_id, principal=p)
        return to_json(cc.recommendations.transition(rec_id, target, actor_type=ActorType.HUMAN,
                                                     actor_id=p.principal_id, principal=p, reason=reason))

    def approve_recommendation(self, p: Principal, rec_id: str, note: str | None = None) -> dict:
        return self._decide_rec(p, rec_id, RecommendationStatus.APPROVED, note)

    def reject_recommendation(self, p: Principal, rec_id: str, reason: str) -> dict:
        return self._decide_rec(p, rec_id, RecommendationStatus.REJECTED, reason)

    def create_change_request(self, p: Principal, rec_id: str, data: Mapping[str, Any]) -> dict:
        _need(p, "changes:write")
        cc = self._cc(p)
        owned(cc.recommendations.repo.find(rec_id), p, "recommendation")
        return to_json(cc.recommendations.create_change_request(rec_id, data, created_by=p.principal_id,
                                                                actor_type=ActorType.HUMAN))

    # -- approvals --------------------------------------------------------------------
    def list_approvals(self, p: Principal, *, status: str | None = None, limit: int = 100,
                       offset: int = 0) -> list[dict]:
        rows = _q(self._cc(p).approvals.repo, p, {"status": status}, "requested_at", limit, offset)
        return [to_json(a) for a in rows]

    def approve_action(self, p: Principal, approval_id: str, note: str | None = None) -> dict:
        cc = self._cc(p)
        owned(cc.approvals.repo.find(approval_id), p, "approval")
        return to_json(cc.approve(approval_id, p, note))        # role-checked by the engine

    def reject_action(self, p: Principal, approval_id: str, reason: str) -> dict:
        cc = self._cc(p)
        owned(cc.approvals.repo.find(approval_id), p, "approval")
        return to_json(cc.reject(approval_id, p, reason))

    # -- KPIs / alerts ----------------------------------------------------------------
    def list_kpis(self, p: Principal) -> list[dict]:
        cc = self._cc(p)
        return [to_json(cc.kpis.latest(p.organization_id, k)) for k in cc.kpis.list_kpis(p.organization_id)]

    def get_kpi_history(self, p: Principal, kpi: str, limit: int = 100) -> list[dict]:
        return [to_json(s) for s in self._cc(p).kpis.history(p.organization_id, kpi, limit)]

    def list_alerts(self, p: Principal, *, status: str | None = None, limit: int = 100,
                    offset: int = 0) -> list[dict]:
        return [to_json(a) for a in _q(self._cc(p).alerts.repo, p, {"status": status}, "created_at", limit, offset)]

    # -- briefs -----------------------------------------------------------------------
    def _latest_doc(self, p: Principal, kind: str, field: str, value: str) -> dict | None:
        rows = [o for o in self._cc(p).outputs.repo.list(lambda o: o.organization_id == p.organization_id
                                                         and o.kind.value == kind and o.validity.value == "VALID")
                if o.content.get(field) == value]
        return self._output(max(rows, key=lambda o: o.created_at), content=True) if rows else None

    def get_daily_brief(self, p: Principal, brief_date: date) -> dict | None:
        return self._latest_doc(p, "DAILY_BRIEF", "brief_date", brief_date.isoformat())

    def get_weekly_review(self, p: Principal, week_start: date) -> dict | None:
        return self._latest_doc(p, "WEEKLY_REVIEW", "review_period_start", week_start.isoformat())

    def generate_daily_brief(self, p: Principal, brief_date: date) -> dict:
        cc = self._cc(p)
        return to_json(BriefingService(cc, cc.runtime).generate_daily_brief(p, brief_date))

    # -- audit / performance ----------------------------------------------------------
    def list_audit_events(self, p: Principal, *, object_type: str | None = None, object_id: str | None = None,
                          limit: int = 200, after_sequence: int = 0) -> list[dict]:
        _need(p, "audit:read")
        store = self._cc(p).audit.store
        if hasattr(store, "page"):
            events = store.page(after_sequence=after_sequence, limit=limit, organization_id=p.organization_id,
                                object_type=object_type, object_id=object_id)
        else:
            events = [e for e in store.events(organization_id=p.organization_id, object_type=object_type,
                                              object_id=object_id) if e.sequence > after_sequence][:limit]
        return [{"event_id": e.event_id, "sequence": e.sequence, "timestamp": e.timestamp.isoformat(),
                 "event_type": e.event_type.value, "actor_type": e.actor_type.value, "actor_id": e.actor_id,
                 "agent_id": e.agent_id, "run_id": e.run_id, "object_type": e.object_type, "object_id": e.object_id,
                 "previous_state": e.previous_state, "new_state": e.new_state, "payload": e.payload}
                for e in events]

    def get_agent_performance(self, p: Principal, agent_id: str, period: str = "30d") -> dict:
        _need(p, "performance:read")
        cc = self._cc(p)
        cc.registry.get_profile(agent_id)
        return to_json(compute_agent_performance(cc.audit.store, agent_id, Period(period), cc.clock()))

    # =====================================================================================
    # Phase 5 additions (HTTP layer). Each reuses the domain checks above.
    # =====================================================================================
    def get_approval(self, p: Principal, approval_id: str) -> dict:
        return to_json(owned(self._cc(p).approvals.repo.find(approval_id), p, "approval"))

    def reconcile_approval(self, p: Principal, approval_id: str, *, outcome: str, evidence_ref: str,
                           note: str) -> dict:
        from .approvals import ReconciliationOutcome
        cc = self._cc(p)
        owned(cc.approvals.repo.find(approval_id), p, "approval")
        return to_json(cc.approvals.reconcile_outcome(approval_id, p, ReconciliationOutcome(outcome),
                                                      evidence_ref=evidence_ref, note=note))

    def request_reexecution(self, p: Principal, approval_id: str) -> dict:
        cc = self._cc(p)
        owned(cc.approvals.repo.find(approval_id), p, "approval")
        return to_json(cc.gateway.request_reexecution(approval_id, p))

    def get_recommendation(self, p: Principal, rec_id: str) -> dict:
        return to_json(owned(self._cc(p).recommendations.repo.find(rec_id), p, "recommendation"))

    def list_change_requests(self, p: Principal, *, recommendation_id: str | None = None, limit: int = 100,
                             offset: int = 0) -> list[dict]:
        repo = self._cc(p).recommendations.changes
        rows = [c for c in repo.list(lambda c: c.organization_id == p.organization_id
                                     and (recommendation_id is None or c.recommendation_id == recommendation_id))]
        rows.sort(key=lambda c: c.created_at)
        return [to_json(c) for c in rows[offset:offset + limit]]

    def get_change_request(self, p: Principal, change_id: str) -> dict:
        cc = self._cc(p)
        cr = owned(cc.recommendations.changes.find(change_id), p, "change request")
        history = [{"from": e.previous_state, "to": e.new_state, "at": e.timestamp.isoformat(), "by": e.actor_id}
                   for e in cc.audit.store.events(object_type="change_request", object_id=change_id)]
        return {**to_json(cr), "lifecycle": history,
                "actual_kpi_change": [to_json(m) for m in cr.actual_kpi_change]}

    def output_versions(self, p: Principal, output_id: str) -> list[dict]:
        """Outputs are immutable: exactly one content version exists per output."""
        o = owned(self._cc(p).outputs.repo.find(output_id), p, "output")
        return [{"version_no": 1, "content_hash": o.content_hash, "created_at": o.created_at.isoformat(),
                 "validity": o.validity.value}]

    def list_evaluations(self, p: Principal, output_id: str) -> list[dict]:
        o = owned(self._cc(p).outputs.repo.find(output_id), p, "output")
        return [{**to_json(e), "composite_score": str(e.composite_score)} for e in o.evaluations]

    def request_revision(self, p: Principal, output_id: str, comment: str) -> dict:
        """A human asks the producing agent to revise. Creates a task for the agent; it does not
        score the output (human evaluation stays separate) and starts no run by itself."""
        _need(p, "outputs:review")
        cc = self._cc(p)
        o = owned(cc.outputs.repo.find(output_id), p, "output")
        if not comment.strip():
            from .errors import ValidationFailedError
            raise ValidationFailedError("a revision request needs a comment")
        task = cc.tasks.create_from_contract({
            "title": f"Revise output {output_id[:8]}", "objective": "Address reviewer feedback",
            "description": comment, "owner": o.agent_id, "priority": "P2", "dependencies": [],
            "expected_output": "Revised output", "completion_criteria": ["Reviewer feedback addressed in a new output"],
            "kpi_affected": [], "risk_level": "GREEN"}, organization_id=p.organization_id, created_by=p.principal_id,
            actor_type=ActorType.HUMAN)
        return {"output_id": output_id, "task_id": task.task_id}

    def kpi_definitions(self, p: Principal) -> list[dict]:
        """Definitions are version-controlled agent configuration (read only here). Values come
        only from sourced KPI snapshots; targets only from organization policy."""
        cc = self._cc(p)
        now = cc.clock()
        out = []
        for agent in cc.registry.list_agents(include_disabled=True):
            for k in agent.kpis:
                snap = cc.kpis.latest(p.organization_id, k.name)
                target = cc.policy.kpi_targets.get(k.name, {}).get("target")
                current = snap.value if snap else None
                age = (now - snap.recorded_at).total_seconds() / 3600 if snap else None
                out.append({"kpi": k.name, "owner_agent": agent.agent_id, "definition": k.definition,
                            "direction": k.direction.value, "unit": k.unit, "definition_source":
                            f"config/agents/{agent.agent_id}.yaml",
                            "current_value": None if current is None else str(current),
                            "observed_at": snap.observed_at if snap else None,
                            "source_reference": snap.source_reference if snap else None,
                            "target": None if target is None else str(target),
                            "variance": None if current is None or target is None else str(current - target),
                            "freshness": {"recorded_at": snap.recorded_at.isoformat() if snap else None,
                                          "age_hours": None if age is None else round(age, 1),
                                          "stale": None if age is None else age > 24 * 7}})
        return out

    def create_conversation(self, p: Principal, *, title: str, primary_agent_id: str | None = None) -> dict:
        cc = self._cc(p)
        if primary_agent_id:
            cc.registry.get_agent(primary_agent_id)
        return to_json(cc.conversations.create(p, title=title, primary_agent_id=primary_agent_id))

    def append_message(self, p: Principal, conversation_id: str, *, content: str,
                       attachment_ids: tuple[str, ...] = ()) -> dict:
        """Append a user message (immutable). Does not start a run; use POST /commands for that."""
        cc = self._cc(p)
        cc.conversations.get(p, conversation_id)
        return to_json(cc.conversations.add_message(conversation_id, sender_type=ActorType.HUMAN,
                                                    sender_id=p.principal_id, content=content,
                                                    attachment_ids=attachment_ids))

    def acknowledge_alert(self, p: Principal, alert_id: str) -> dict:
        _need(p, "alerts:write")
        cc = self._cc(p)
        owned(cc.alerts.repo.find(alert_id), p, "alert")
        return to_json(cc.alerts.acknowledge(alert_id, p))

    def resolve_alert(self, p: Principal, alert_id: str, note: str) -> dict:
        _need(p, "alerts:write")
        cc = self._cc(p)
        owned(cc.alerts.repo.find(alert_id), p, "alert")
        return to_json(cc.alerts.resolve(alert_id, p, note))

    def get_command(self, p: Principal, command_id: str) -> dict:
        queue = self.commands.queue
        job = None
        if queue is not None:
            try:
                job = queue.get(command_id)
            except NotFoundError:
                job = None
        if job is not None and job.organization_id == p.organization_id:
            run = self._cc(p).runs.repo.find(job.run_id)
            return {"command_id": job.job_id, "status": job.status.value, "run_id": job.run_id,
                    "run_status": run.status.value if run else None, "attempts": job.attempts,
                    "created_at": job.created_at.isoformat()}
        run = self._cc(p).runs.repo.find(command_id)          # synchronous mode: command id = run id
        owned(run, p, "command")
        return {"command_id": run.run_id, "status": run.status.value, "run_id": run.run_id,
                "run_status": run.status.value, "attempts": 1, "created_at": run.created_at.isoformat()}

    def connector_status(self, p: Principal) -> list[dict]:
        cc = self._cc(p)
        repos = cc.repositories
        if repos is None or getattr(repos, "db", None) is None:
            from .connectors import CONTRACTS
            return [{"view": f"connector.{v}", "tool": c.tool, "configured": False, "state": "UNAVAILABLE",
                     "contract": "n/a", "rows": 0, "freshest_record": None, "last_successful_refresh": None,
                     "latest_error": "no database configured"} for v, c in CONTRACTS.items()]
        from .pg.diagnostics import connector_status
        return connector_status(repos.db, repos.org_uuid, cc.clock())

    def events_since(self, p: Principal, *, after: int = 0, limit: int = 100) -> dict:
        """Organization-scoped realtime events derived from the audit log; `after` is the cursor."""
        from .realtime import to_realtime
        store = self._cc(p).audit.store
        items, cursor = [], after
        while len(items) < limit:
            batch = store.page(after_sequence=cursor, limit=200, organization_id=p.organization_id)
            if not batch:
                break
            for e in batch:
                cursor = e.sequence
                rt = to_realtime(e)
                if rt is not None and rt.organization_id == p.organization_id:
                    items.append({"id": str(e.sequence), "event_type": rt.event_type,
                                  "timestamp": rt.timestamp.isoformat(), "object_type": rt.object_type,
                                  "object_id": rt.object_id, "payload_summary": rt.payload_summary})
                    if len(items) >= limit:
                        break
        return {"items": items, "next_cursor": cursor}

    def executive_dashboard(self, p: Principal) -> dict:
        from .dashboards import executive_dashboard
        return executive_dashboard(self, p)

    def agent_dashboard(self, p: Principal, agent_id: str) -> dict:
        from .dashboards import agent_dashboard
        return agent_dashboard(self, p, agent_id)

