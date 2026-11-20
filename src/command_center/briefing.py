"""Daily brief (Part 42) and weekly review (Part 43) generation.

Flow
 1 idempotency: one brief per (organization, date) / one review per (organization, week)
 2 Data Analyst validates freshness first
 3 relevant specialists run, each with only its own permitted context
 4 a failed specialist marks its domain UNAVAILABLE; the brief still proceeds
 5 the application computes data quality from what actually happened (failed runs,
   UNAVAILABLE / STALE tool results); the Chief of Staff receives it with the summaries
 6 the Chief of Staff writes the DAILY_BRIEF / WEEKLY_REVIEW document; it is validated
   (priority limits, sources, data quality not understated, not authoritative when data is
   degraded) and persisted as an output
 7 critical issues publish an alert

Fan-out is bounded by these fixed agent lists (not by model choice). Specialist runs are
workflow-owned runs, so the Chief of Staff's model-initiated delegation limits are unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

from .agents import RunContext
from .alerts import Severity
from .audit import EventType
from .common import Principal
from .context import ContextItem
from .idempotency import idempotency_key
from .outputs import OutputKind
from .sources import AuthorityLevel, SourceRecord, SourceType
from .state_machines import RunStatus

DAILY_SPECIALISTS = ("cfo", "sales_director", "operations", "customer_success", "marketing_director")
DAILY_DOMAINS = {"cfo": ("cash", "costs", "revenue"), "sales_director": ("pipeline", "revenue"),
                 "operations": ("operations",), "customer_success": ("customers",),
                 "marketing_director": ("pipeline",)}
WEEKLY_CORE = ("sales_director", "cfo", "operations", "customer_success", "marketing_director", "legal_risk")
WEEKLY_DOMAINS = {"sales_director": ("revenue", "pipeline"), "cfo": ("profit", "cash", "costs"),
                  "operations": ("delivery",), "customer_success": ("customers",), "marketing_director": ("pipeline",),
                  "legal_risk": ("risk",), "product_analyst": (), "procurement": ("costs",),
                  "people_performance": ("people",)}
_SEVERITY = {"GOOD": 0, "DEGRADED": 1, "POOR": 2, "UNKNOWN": 2}


@dataclass(frozen=True)
class SpecialistResult:
    agent_id: str
    status: RunStatus
    run_id: str
    output_id: str | None
    error: str | None
    summary: str | None


@dataclass(frozen=True)
class BriefResult:
    status: str                       # GENERATED | DUPLICATE | FAILED
    output_id: str | None
    data_quality: Mapping[str, Any]
    specialists: tuple[SpecialistResult, ...]
    invoked_agents: tuple[str, ...]
    alert_ids: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class BriefingService:
    cc: Any
    runtime: Any

    # -- shared ------------------------------------------------------------------------
    def _specialist(self, agent_id: str, principal: Principal, message: str, **ctx_kw: Any) -> SpecialistResult:
        cc = self.cc
        try:
            run = cc.runs.create_run(agent_id, principal, message)
        except Exception as exc:   # budget, permission, disabled agent: domain becomes unavailable
            return SpecialistResult(agent_id, RunStatus.FAILED, "", None, f"{type(exc).__name__}: {exc}", None)
        ctx = RunContext("Scheduled management review", (), (), message, principal, requested_agent_id=agent_id, **ctx_kw)
        res = self.runtime.execute(run.run_id, ctx, message=message)
        summary = None
        if res.output_id:
            out = cc.outputs.get(res.output_id)
            if out.validity.value == "VALID":
                summary = out.content.get("executive_summary")
        return SpecialistResult(agent_id, res.status, run.run_id, res.output_id, res.error, summary)

    def _data_quality(self, data: SpecialistResult, specialists: list[SpecialistResult]) -> dict[str, Any]:
        cc = self.cc
        run_ids = {s.run_id for s in specialists + [data] if s.run_id}
        stale, missing = [], []
        for e in cc.audit.store.events(event_type=[EventType.TOOL_EXECUTED, EventType.TOOL_FAILED]):
            if e.run_id in run_ids:
                st = e.payload.get("status")
                if st == "UNAVAILABLE":
                    missing.append(f"{e.object_id} (requested by {e.agent_id})")
                elif st == "STALE":
                    stale.append({"source": f"{e.object_id} (requested by {e.agent_id})", "last_updated": None})
        failed = [s for s in specialists if s.status not in (RunStatus.SUCCEEDED, RunStatus.APPROVAL_REQUIRED)]
        missing += [f"{s.agent_id} review ({s.error or s.status.value})" for s in failed]
        data_ok = data.status in (RunStatus.SUCCEEDED, RunStatus.APPROVAL_REQUIRED)
        if not data_ok:
            missing.append(f"data validation ({data.error or data.status.value})")
        if not data_ok or (specialists and len(failed) * 2 >= len(specialists)):
            overall = "POOR"
        elif missing or stale:
            overall = "DEGRADED"
        else:
            overall = "GOOD"
        notes = "Computed by the application from run and tool outcomes."
        if not data_ok:
            notes += " Data freshness could not be validated; figures are unverified."
        return {"overall_status": overall, "stale_sources": stale, "missing_sources": sorted(set(missing)),
                "conflicting_sources": [], "unverified_metrics": [] if data_ok else ["all"], "notes": notes}

    def _summary_items(self, results: list[SpecialistResult], dq: Mapping[str, Any], key: str) -> list[ContextItem]:
        items = []
        for r in results:
            if r.output_id and r.summary:
                text = f"{r.agent_id} summary: {r.summary}"
                src = SourceRecord(f"out:{r.output_id}", SourceType.AGENT_OUTPUT, f"{r.agent_id} output",
                                   f"outputs:{r.output_id}", None, AuthorityLevel.SECONDARY)
            else:
                text = f"{r.agent_id}: UNAVAILABLE ({r.error or r.status.value}). Mark its domains UNAVAILABLE."
                src = SourceRecord(f"run:{r.run_id or r.agent_id}", SourceType.SYSTEM_CONFIG, f"{r.agent_id} run",
                                   f"agent_runs:{r.run_id or 'not-created'}", None, AuthorityLevel.AUTHORITATIVE)
            items.append(ContextItem("specialist", text, src, 0))
        items.append(ContextItem("data_quality", "Application-computed data quality (report it as is, or more "
                                 "severe; never better): " + json.dumps(dq),
                                 SourceRecord(f"dq:{key}", SourceType.SYSTEM_CONFIG, "Data quality assessment",
                                              f"data_quality:{key}", None, AuthorityLevel.AUTHORITATIVE), 0))
        return items

    @staticmethod
    def _dq_floor(computed: Mapping[str, Any]):
        def check(doc: Mapping[str, Any]) -> list[str]:
            dq = doc.get("data_quality")
            if not isinstance(dq, Mapping):
                return ["data_quality missing"]
            if _SEVERITY.get(dq.get("overall_status"), 2) < _SEVERITY[computed["overall_status"]]:
                return [f"document reports data quality {dq.get('overall_status')} but the application computed "
                        f"{computed['overall_status']}"]
            missing = set(computed["missing_sources"]) - set(dq.get("missing_sources", []))
            return [f"document omits missing sources: {sorted(missing)}"] if missing else []
        return check

    def _alerts(self, key: str, dq: Mapping[str, Any], critical: list[str], source_agent: str) -> list[str]:
        org = self.cc.policy.organization_id
        ids = []
        if critical:
            ids.append(self.cc.alerts.create(organization_id=org, severity=Severity.CRITICAL, category="BRIEF",
                                             title=f"Critical items in {key}", description="; ".join(critical)[:2000],
                                             source_agent=source_agent, dedupe_key=f"{key}:critical").alert_id)
        if dq["overall_status"] != "GOOD":
            sev = Severity.WARNING if dq["overall_status"] == "DEGRADED" else Severity.CRITICAL
            ids.append(self.cc.alerts.create(organization_id=org, severity=sev, category="DATA_QUALITY",
                                             title=f"{key}: data quality {dq['overall_status']}",
                                             description="; ".join(dq["missing_sources"])[:2000] or dq["notes"],
                                             source_agent="data_analyst", dedupe_key=f"{key}:dq").alert_id)
        return ids

    # -- daily -------------------------------------------------------------------------
    def generate_daily_brief(self, principal: Principal, brief_date: date) -> BriefResult:
        cc, org = self.cc, self.cc.policy.organization_id
        key = idempotency_key(org, "daily_brief", brief_date.isoformat())
        fresh, rec = cc.idempotency.claim(org, key)
        if not fresh:
            return BriefResult("DUPLICATE", rec.result_ref, {}, (), ())
        day = brief_date.isoformat()
        data = self._specialist("data_analyst", principal, f"Daily data validation for {day}: check freshness and "
                                "quality of the data behind today's management brief.")
        specialists = [self._specialist(a, principal, f"Daily {', '.join(DAILY_DOMAINS[a])} review for {day}: "
                                        "return a concise structured ANALYSIS.") for a in DAILY_SPECIALISTS]
        dq = self._data_quality(data, specialists)
        invoked = ("data_analyst",) + DAILY_SPECIALISTS + ("chief_of_staff",)
        cos = cc.runs.create_run("chief_of_staff", principal, f"Daily brief {day}")
        ctx = RunContext("Daily brief synthesis", (), (), f"Daily brief {day}", principal,
                         requested_agent_id="chief_of_staff")
        res = self.runtime.execute(
            cos.run_id, ctx, output_kind=OutputKind.DAILY_BRIEF,
            message=(f"Write the DAILY_BRIEF for {day} (brief_date {day}, organization_id {org}, agent_id "
                     "chief_of_staff). Synthesize the specialist summaries; do not concatenate them. At most 7 "
                     "priorities, prefer 5. Domains whose specialist was unavailable must say so. Include "
                     "data_quality."),
            extra_items=self._summary_items([data] + specialists, dq, f"daily:{day}"),
            extra_checks=self._dq_floor(dq))
        if res.status is not RunStatus.SUCCEEDED or not res.output_id:
            return BriefResult("FAILED", res.output_id, dq, tuple([data] + specialists), invoked, (), res.error)
        cc.idempotency.set_result(org, key, res.output_id)
        brief = cc.outputs.get(res.output_id).content
        critical = [f"{c['name']}: {c['summary']}" for c in brief["business_health"]["components"] if c["status"] == "RED"]
        alerts = self._alerts(f"daily brief {day}", dq, critical, "chief_of_staff")
        return BriefResult("GENERATED", res.output_id, dq, tuple([data] + specialists), invoked, tuple(alerts))

    # -- weekly ------------------------------------------------------------------------
    def weekly_participants(self, principal: Principal, start: date, end: date) -> tuple[str, ...]:
        """Core specialists plus conditional ones with evidence of relevance in the period."""
        cc = self.cc
        lo = start.isoformat()
        events = [e for e in cc.audit.store.events() if e.timestamp.date().isoformat() >= lo
                  and e.timestamp.date() <= end]
        agents_active = {e.agent_id for e in events}
        out = list(WEEKLY_CORE)
        product_kpis = {k.name for k in cc.registry.get_profile("product_analyst").kpis}
        if "product_analyst" in agents_active or any(cc.kpis.latest(cc.policy.organization_id, k) for k in product_kpis):
            out.append("product_analyst")
        if "procurement" in agents_active or any(a.tool_name == "create_purchase_order"
                                                 for a in cc.approvals.repo.list()):
            out.append("procurement")
        if cc.policy.privacy_gate_open("read_people_summary") and principal.has("view:people_data"):
            out.append("people_performance")
        return tuple(out)

    def generate_weekly_review(self, principal: Principal, week_start: date) -> BriefResult:
        cc, org = self.cc, self.cc.policy.organization_id
        week_end = week_start + timedelta(days=6)
        key = idempotency_key(org, "weekly_review", week_start.isoformat())
        fresh, rec = cc.idempotency.claim(org, key)
        if not fresh:
            return BriefResult("DUPLICATE", rec.result_ref, {}, (), ())
        span = f"{week_start.isoformat()}..{week_end.isoformat()}"
        participants = self.weekly_participants(principal, week_start, week_end)
        data = self._specialist("data_analyst", principal, f"Weekly data validation for {span}.")
        specialists = [self._specialist(a, principal, f"Weekly {', '.join(WEEKLY_DOMAINS[a]) or 'product'} review "
                                        f"for {span}: structured ANALYSIS.") for a in participants]
        dq = self._data_quality(data, specialists)
        cos = cc.runs.create_run("chief_of_staff", principal, f"Weekly management review {span}")
        ctx = RunContext("Weekly management review", (), (), f"Weekly review {span}", principal,
                         requested_agent_id="chief_of_staff")
        people_included = "people_performance" in participants
        check_dq = self._dq_floor(dq)

        def checks(doc: Mapping[str, Any]) -> list[str]:
            errs = check_dq(doc)
            if doc.get("authoritative") and dq["overall_status"] != "GOOD":
                errs.append("review cannot be authoritative: application data quality is " + dq["overall_status"])
            if not people_included and doc.get("people", {}).get("status") != "UNAVAILABLE":
                errs.append("people domain must be UNAVAILABLE: People agent not authorized for this review")
            return errs

        res = self.runtime.execute(
            cos.run_id, ctx, output_kind=OutputKind.WEEKLY_REVIEW,
            message=(f"Write the WEEKLY_REVIEW for {span} (review_period_start {week_start.isoformat()}, "
                     f"review_period_end {week_end.isoformat()}, organization_id {org}, generated_by_agent "
                     "chief_of_staff). Include data_quality, unresolved_items, watchlist, decisions_required and "
                     "priorities_next_week (max 7, prefer 5). At most 5 recommendations. Set authoritative=false "
                     "unless data quality is GOOD and every domain is available."),
            extra_items=self._summary_items([data] + specialists, dq, f"weekly:{week_start.isoformat()}"),
            extra_checks=checks)
        invoked = ("data_analyst",) + participants + ("chief_of_staff",)
        if res.status is not RunStatus.SUCCEEDED or not res.output_id:
            return BriefResult("FAILED", res.output_id, dq, tuple([data] + specialists), invoked, (), res.error)
        cc.idempotency.set_result(org, key, res.output_id)
        review = cc.outputs.get(res.output_id).content
        critical = [f"{k}: {review[k]['summary']}" for k in ("revenue", "profit", "cash", "pipeline", "delivery",
                                                             "customers", "costs", "people", "risk")
                    if review[k]["status"] == "RED"]
        alerts = self._alerts(f"weekly review {week_start.isoformat()}", dq, critical, "chief_of_staff")
        return BriefResult("GENERATED", res.output_id, dq, tuple([data] + specialists), invoked, tuple(alerts))


# ---------------------------------------------------------------------------------------
# scheduler dispatch (Parts 37-38)
# ---------------------------------------------------------------------------------------
@dataclass
class ScheduledWorkflowDispatcher:
    """Maps scheduled workflow names to work. Runs on behalf of an explicit scheduler principal
    (a service identity the organization configures with run_agent permissions); it is never
    an implicit superuser."""

    cc: Any
    runtime: Any
    run_as: Principal

    def __call__(self, job: Any, scheduled_for: Any) -> str | None:
        from zoneinfo import ZoneInfo
        from .workflow_catalog import REVIEW_WORKFLOW_AGENTS
        local = scheduled_for.astimezone(ZoneInfo(job.timezone))
        svc = BriefingService(self.cc, self.runtime)
        if job.workflow == "daily_brief":
            return svc.generate_daily_brief(self.run_as, local.date()).output_id
        if job.workflow == "weekly_management_review":
            return svc.generate_weekly_review(self.run_as, local.date() - timedelta(days=local.weekday())).output_id
        agent = REVIEW_WORKFLOW_AGENTS.get(job.workflow)
        if agent is None:
            raise ValueError(f"no dispatcher for workflow {job.workflow!r}")
        message = (f"Scheduled {job.workflow.replace('_', ' ')} for {local.date().isoformat()}: return a structured "
                   "summary; report missing or stale data explicitly.")
        if job.workflow == "end_of_day_reconciliation":
            message = (f"End-of-day review for {local.date().isoformat()}: completed, incomplete, blocked, unexpected "
                       "events, KPI changes, decisions pending, tomorrow's priorities; identify missed commitments "
                       "by humans and agents.")
        return self._specialist_run(agent, message)

    def _specialist_run(self, agent: str, message: str) -> str:
        run = self.cc.runs.create_run(agent, self.run_as, message[:200])
        self.runtime.execute(run.run_id, RunContext("Scheduled run", (), (), message, self.run_as,
                                                    requested_agent_id=agent), message=message)
        return run.run_id
