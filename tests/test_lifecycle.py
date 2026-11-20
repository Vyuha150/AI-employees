from datetime import timedelta
from decimal import Decimal

import pytest

from command_center.agents import AgentRegistry
from command_center.audit import AuditLogger, InMemoryAuditStore
from command_center.briefs import validate_daily_brief, validate_weekly_review
from command_center.common import ActorType, Principal
from command_center.errors import PermissionDeniedError, PolicyViolationError, ValidationFailedError
from command_center.evaluation import OutputEvaluation, ReviewDecision
from command_center.memory import MemoryService
from command_center.performance import Period, compute_agent_performance
from command_center.recommendations import OutcomeStatus, RecommendationService
from command_center.sources import SourceCatalog, SourceRecord, SourceType
from command_center.state_machines import ChangeStatus, MemoryStatus, RecommendationStatus

from .conftest import ORG, FakeClock, human

REG = AgentRegistry.load_dir()
REVIEWER = human("founder", roles={"FOUNDER"})
SOURCES = SourceCatalog.from_response({"sources": [
    {"id": "s1", "type": "COMPANY_DATABASE", "reference": "crm:lead_response_times"},
    {"id": "s2", "type": "DATABASE", "reference": "legacy-type alias"},
]})

REC = {
    "problem_or_opportunity": "Median first response to inbound leads is 26 hours.",
    "evidence": ["CRM export: median 26h over 90 days"],
    "evidence_source_ids": ["s1"],
    "recommendation": "Route inbound leads to the SDR queue with a 1-hour response SLA.",
    "why_now": "Inbound volume doubled this quarter.",
    "expected_impact": "Higher lead-to-meeting rate.",
    "impact_category": "REVENUE",
    "estimated_value": None, "estimated_cost": None,
    "effort": "LOW", "urgency": "HIGH", "dependencies": [], "risks": ["SDR capacity"],
    "reversible": True, "confidence": "MEDIUM", "suggested_owner": "sales_director",
    "kpis_affected": ["lead_to_meeting_rate"],
}


@pytest.fixture
def svc():
    clock = FakeClock()
    store = InMemoryAuditStore()
    return RecommendationService(AuditLogger(store, clock), clock=clock), clock, store


def test_source_types_normalize():
    assert SOURCES.get("s2").source_type is SourceType.COMPANY_DATABASE
    assert SourceRecord.from_wire({"id": "m", "type": "MEMORY", "reference": "x"}).is_primary_evidence is False


def approved_rec(rs):
    rec = rs.create(REC, organization_id=ORG, source_agent="lead_generation", run_id="run_1", sources=SOURCES)
    rs.transition(rec.recommendation_id, RecommendationStatus.UNDER_REVIEW, actor_type=ActorType.SYSTEM,
                  actor_id="system")
    return rs.transition(rec.recommendation_id, RecommendationStatus.APPROVED, actor_type=ActorType.HUMAN,
                         actor_id="founder", principal=REVIEWER)


def test_agent_cannot_approve_recommendation(svc):
    rs, _, _ = svc
    rec = rs.create(REC, organization_id=ORG, source_agent="lead_generation", run_id="run_1", sources=SOURCES)
    rs.transition(rec.recommendation_id, RecommendationStatus.UNDER_REVIEW, actor_type=ActorType.SYSTEM,
                  actor_id="system")
    with pytest.raises(PermissionDeniedError):
        rs.transition(rec.recommendation_id, RecommendationStatus.APPROVED, actor_type=ActorType.AGENT,
                      actor_id="chief_of_staff")


def test_rejected_recommendation_needs_new_evidence(svc):
    rs, _, _ = svc
    rec = rs.create(REC, organization_id=ORG, source_agent="lead_generation", run_id="run_1", sources=SOURCES)
    rs.transition(rec.recommendation_id, RecommendationStatus.REJECTED, actor_type=ActorType.HUMAN,
                  actor_id="founder", principal=REVIEWER, reason="SDR team is at capacity")
    reworded = {**REC, "recommendation": "route inbound leads to the SDR queue, with a 1 hour response SLA!"}
    with pytest.raises(PolicyViolationError, match="no new evidence"):
        rs.create(reworded, organization_id=ORG, source_agent="lead_generation", run_id="run_2", sources=SOURCES)
    with_new = {**REC, "evidence": REC["evidence"] + ["Two SDRs hired this month"]}
    assert rs.create(with_new, organization_id=ORG, source_agent="lead_generation", run_id="run_3",
                     sources=SOURCES).status is RecommendationStatus.PROPOSED


def test_unknown_evidence_source_rejected(svc):
    rs, _, _ = svc
    with pytest.raises(ValidationFailedError):
        rs.create({**REC, "evidence_source_ids": ["nope"]}, organization_id=ORG, source_agent="x", run_id=None,
                  sources=SOURCES)


CHANGE = {
    "current_state": "Leads wait 26h", "problem": "Slow response", "desired_state": "Response within 1h",
    "change": "SDR queue with SLA", "expected_benefit": "More meetings", "risk": "SDR overload",
    "implementation_steps": ["Configure routing", "Set SLA alerts"], "validation_method": "Compare 30-day cohorts",
    "rollback_plan": "Restore round-robin routing", "owner": "sales_director",
    "expected_kpi_change": [{
        "kpi": "lead_to_meeting_rate", "unit": "percent", "direction": "HIGHER_IS_BETTER",
        "baseline": "20", "target": "23", "expected_value": "23",
        "measurement_start": "2026-11-21T00:00:00+00:00", "measurement_end": "2026-12-21T00:00:00+00:00"}],
}


def drive_change_to_completed(rs, rec):
    cr = rs.create_change_request(rec.recommendation_id, CHANGE, created_by="sales_director",
                                  actor_type=ActorType.AGENT)
    rs.transition_change(cr.change_id, ChangeStatus.APPROVED, actor_type=ActorType.HUMAN, actor_id="founder",
                         principal=REVIEWER)
    for s in (ChangeStatus.PLANNING, ChangeStatus.IMPLEMENTATION, ChangeStatus.TESTING, ChangeStatus.REVIEW,
              ChangeStatus.COMPLETED):
        rs.transition_change(cr.change_id, s, actor_type=ActorType.AGENT, actor_id="sales_director")
    return cr


def test_outcome_loop_predicted_15_actual_9_is_partial(svc):
    """+15% predicted (20 -> 23), +9% actual (20 -> 21.8): PARTIAL; both values stored."""
    rs, clock, store = svc
    rec = approved_rec(rs)
    cr = drive_change_to_completed(rs, rec)
    assert cr.expected_kpi_change[0].outcome_status is OutcomeStatus.NOT_MEASURED
    assert cr.actual_kpi_change == ()

    with pytest.raises(PolicyViolationError, match="window has not ended"):
        rs.record_outcome(cr.change_id, "lead_to_meeting_rate", actual="21.8", sources=SOURCES,
                          source_ids=["s1"], measured_by=REVIEWER)
    clock.advance(days=32)
    with pytest.raises(ValidationFailedError):
        rs.record_outcome(cr.change_id, "lead_to_meeting_rate", actual="21.8", sources=SOURCES,
                          source_ids=[], measured_by=REVIEWER)  # no invented actuals
    cr = rs.record_outcome(cr.change_id, "lead_to_meeting_rate", actual="21.8", sources=SOURCES,
                           source_ids=["s1"], measured_by=REVIEWER)
    m = cr.expected_kpi_change[0]
    assert m.expected_value == Decimal("23") and m.actual == Decimal("21.8")
    assert m.variance == Decimal("-1.2") and m.outcome_status is OutcomeStatus.PARTIAL
    assert len(cr.actual_kpi_change) == 1


def test_actuals_cannot_be_supplied_up_front(svc):
    rs, _, _ = svc
    rec = approved_rec(rs)
    bad = {**CHANGE, "expected_kpi_change": [{**CHANGE["expected_kpi_change"][0], "actual": "30"}]}
    with pytest.raises(ValidationFailedError):
        rs.create_change_request(rec.recommendation_id, bad, created_by="x", actor_type=ActorType.AGENT)


def test_change_requires_approved_recommendation_and_rollback_plan(svc):
    rs, _, _ = svc
    rec = rs.create(REC, organization_id=ORG, source_agent="lead_generation", run_id="run_1", sources=SOURCES)
    with pytest.raises(PolicyViolationError):
        rs.create_change_request(rec.recommendation_id, CHANGE, created_by="x", actor_type=ActorType.AGENT)
    rec = approved_rec(rs)
    with pytest.raises(ValidationFailedError, match="rollback_plan"):
        rs.create_change_request(rec.recommendation_id, {**CHANGE, "rollback_plan": ""}, created_by="x",
                                 actor_type=ActorType.AGENT)


def test_change_human_gates(svc):
    rs, _, _ = svc
    cr = rs.create_change_request(approved_rec(rs).recommendation_id, CHANGE, created_by="x",
                                  actor_type=ActorType.AGENT)
    with pytest.raises(PermissionDeniedError):
        rs.transition_change(cr.change_id, ChangeStatus.APPROVED, actor_type=ActorType.AGENT, actor_id="cos")


def test_cannot_verify_or_close_without_measurement(svc):
    rs, _, _ = svc
    rec = approved_rec(rs)
    cr = drive_change_to_completed(rs, rec)
    rs.transition_change(cr.change_id, ChangeStatus.IMPACT_MEASUREMENT, actor_type=ActorType.AGENT, actor_id="x")
    with pytest.raises(ValidationFailedError):
        rs.transition_change(cr.change_id, ChangeStatus.CLOSED, actor_type=ActorType.HUMAN, actor_id="founder",
                             principal=REVIEWER)
    for s in (RecommendationStatus.IMPLEMENTATION, RecommendationStatus.IMPLEMENTED, RecommendationStatus.IMPACT_REVIEW):
        rs.transition(rec.recommendation_id, s, actor_type=ActorType.AGENT, actor_id="x")
    with pytest.raises(ValidationFailedError, match="measured outcome"):
        rs.transition(rec.recommendation_id, RecommendationStatus.VERIFIED, actor_type=ActorType.HUMAN,
                      actor_id="founder", principal=REVIEWER)


# ---- memory --------------------------------------------------------------------------------
@pytest.fixture
def mem():
    clock = FakeClock()
    return MemoryService(AuditLogger(InMemoryAuditStore(), clock), clock=clock), clock


CANDIDATE = {"content": "Net-30 is the approved default payment term.", "category": "BUSINESS_RULE",
             "source_ids": ["s1"], "rationale": "Policy approved by founder", "scope": "ORGANIZATION"}


def test_memory_is_not_context_until_accepted(mem):
    ms, _ = mem
    cfo = REG.get_agent("cfo")
    m = ms.propose(CANDIDATE, agent=cfo, organization_id=ORG, run_id="r", sources=SOURCES)
    assert m.status is MemoryStatus.PROPOSED and ms.context_for(cfo, ORG) == []
    ms.accept(m.memory_id, REVIEWER)
    assert [x.memory_id for x in ms.context_for(cfo, ORG)] == [m.memory_id]
    ms.invalidate(m.memory_id, REVIEWER, "policy changed to net-45")
    assert ms.context_for(cfo, ORG) == []


def test_memory_rules(mem):
    ms, clock = mem
    cfo, sdr = REG.get_agent("cfo"), REG.get_agent("lead_generation")
    with pytest.raises(ValidationFailedError):
        ms.propose({**CANDIDATE, "source_ids": []}, agent=cfo, organization_id=ORG, run_id="r", sources=SOURCES)
    with pytest.raises(PermissionDeniedError):  # SDR has no ORGANIZATION memory scope
        ms.propose(CANDIDATE, agent=sdr, organization_id=ORG, run_id="r", sources=SOURCES)
    m = ms.propose(CANDIDATE, agent=cfo, organization_id=ORG, run_id="r", sources=SOURCES)
    with pytest.raises(PermissionDeniedError):  # agents cannot accept memory
        ms.accept(m.memory_id, Principal("cfo", ORG, ActorType.AGENT))
    cust = ms.propose({**CANDIDATE, "scope": "CUSTOMER", "scope_ref": "cust_9"}, agent=cfo, organization_id=ORG,
                      run_id="r", sources=SOURCES)
    ms.accept(cust.memory_id, REVIEWER)
    assert cust.memory_id not in {x.memory_id for x in ms.context_for(cfo, ORG)}
    assert cust.memory_id in {x.memory_id for x in ms.context_for(cfo, ORG, customer_ids=["cust_9"])}
    # defaults to the narrowest scope
    own = ms.propose({k: v for k, v in CANDIDATE.items() if k != "scope"}, agent=cfo, organization_id=ORG,
                     run_id="r", sources=SOURCES)
    assert own.scope.value == "AGENT" and own.scope_ref == "cfo"
    # expiry
    short = ms.propose({**CANDIDATE, "expires_at": (clock() + timedelta(days=1)).isoformat()}, agent=cfo,
                       organization_id=ORG, run_id="r", sources=SOURCES)
    ms.accept(short.memory_id, REVIEWER)
    clock.advance(days=2)
    assert short.memory_id not in {x.memory_id for x in ms.context_for(cfo, ORG)}
    assert short.memory_id in {x.memory_id for x in ms.expire_due()}


# ---- evaluation ----------------------------------------------------------------------------
def test_composite_and_decision_are_independent():
    perfect = OutputEvaluation("o", "founder", FakeClock()(), 5, 5, 5, 5, 5, ReviewDecision.NEEDS_REVISION,
                               "great but wrong customer")
    assert perfect.composite_score == Decimal("5.00") and not perfect.approved and perfect.needs_revision
    weighted = OutputEvaluation("o", "founder", FakeClock()(), 5, 1, 1, 5, 3, ReviewDecision.APPROVED)
    assert weighted.composite_score == Decimal("3.20")  # .2*5 + .15 + .15 + .2*5 + .3*3
    with pytest.raises(ValidationFailedError):
        OutputEvaluation("o", "f", FakeClock()(), 6, 5, 5, 5, 5, ReviewDecision.APPROVED)
    with pytest.raises(ValidationFailedError):
        OutputEvaluation("o", "f", FakeClock()(), 1, 1, 1, 1, 1, ReviewDecision.REJECTED)  # comment required


# ---- performance ---------------------------------------------------------------------------
def test_performance_reports_none_without_data():
    p = compute_agent_performance(InMemoryAuditStore(), "cfo", Period.D30, FakeClock()())
    assert p.tasks_completed == 0 and p.completion_rate is None and p.average_quality_score is None
    assert p.estimated_cost is None and p.cost_per_completed_task is None and p.run_failure_rate is None
    assert {x.days for x in Period} == {7, 30, 90}


# ---- briefs --------------------------------------------------------------------------------
def section(text="ok", src=None):
    item = {"text": text, "kind": "FACT"}
    if src:
        item["source_ids"] = src
    return {"summary": text, "items": [item]}


def brief(n_priorities=3, src="s1"):
    return {
        "brief_date": "2026-11-20", "organization_id": ORG, "agent_id": "chief_of_staff",
        "executive_summary": "Cash is fine; two deals at risk.",
        "business_health": {"score": None, "components": [{"name": "cash", "status": "GREEN", "summary": "ok"}]},
        **{k: section(src=[src]) for k in ("revenue", "cash", "pipeline", "operations", "customers", "costs", "risk")},
        "todays_priorities": [{"title": f"P{i}", "why_it_matters": "x", "owner": "founder",
                               "source_agent": "sales_director", "urgency": "HIGH", "expected_impact": "x",
                               "deadline": None, "status": "NOT_STARTED"} for i in range(n_priorities)],
        "decisions_required": [], "ai_actions_in_progress": [], "watchlist": [],
        "sources": [{"id": "s1", "type": "COMPANY_DATABASE", "reference": "finance:cash"}],
    }


def test_daily_brief_contract():
    agents = [p.agent_id for p in REG.list_agents()]
    assert validate_daily_brief(brief(), agents).ok
    six = validate_daily_brief(brief(6), agents)
    assert six.ok and six.warnings
    assert not validate_daily_brief(brief(8), agents).ok          # max 7
    assert not validate_daily_brief(brief(src="ghost"), agents).ok  # unresolved source
    b = brief()
    b["todays_priorities"][0]["source_agent"] = "generic_agent"
    assert not validate_daily_brief(b, agents).ok


def test_weekly_review_contract():
    w = {"week_start": "2026-11-16", "week_end": "2026-11-20", "organization_id": ORG, "agent_id": "chief_of_staff",
         "overall_health": {"score": 72, "components": []},
         **{k: section() for k in ("revenue", "profit", "cash", "pipeline", "delivery", "customers", "costs",
                                   "people", "risk")},
         "top_wins": [], "top_failures": [],
         "root_causes": [{"problem": "Late delivery", "cause": "Vendor delay", "status": "VERIFIED"}],
         "decisions": [], "sources": []}
    r = validate_weekly_review(w)
    assert not r.ok and "VERIFIED root cause" in r.errors[0]
    w["root_causes"][0]["status"] = "SUSPECTED"
    assert validate_weekly_review(w).ok


# ---- tasks ---------------------------------------------------------------------------------
def test_task_rules():
    from datetime import date

    from command_center.state_machines import TaskStatus
    from command_center.tasks import TaskService

    clock = FakeClock()
    store = InMemoryAuditStore()
    ts = TaskService(AuditLogger(store, clock), clock=clock)
    base = {"title": "Collect overdue AR", "objective": "Cash", "description": "Call two accounts", "owner": "cfo",
            "priority": "P0", "expected_output": "Payment dates", "completion_criteria": ["Dates agreed"],
            "risk_level": "GREEN", "due_date": "2026-11-18"}
    with pytest.raises(ValidationFailedError, match="completion_criteria"):
        ts.create_from_contract({**base, "completion_criteria": []}, organization_id=ORG, created_by="cfo",
                                actor_type=ActorType.AGENT)
    dep = ts.create_from_contract(base, organization_id=ORG, created_by="cfo", actor_type=ActorType.AGENT)
    t = ts.create_from_contract({**base, "dependencies": [dep.task_id]}, organization_id=ORG, created_by="cfo",
                                actor_type=ActorType.AGENT)
    ts.transition(t.task_id, TaskStatus.IN_PROGRESS, actor_type=ActorType.AGENT, actor_id="cfo")
    with pytest.raises(ValidationFailedError, match="reason"):
        ts.transition(t.task_id, TaskStatus.BLOCKED, actor_type=ActorType.AGENT, actor_id="cfo")
    with pytest.raises(ValidationFailedError, match="open dependencies"):
        ts.transition(t.task_id, TaskStatus.COMPLETED, actor_type=ActorType.AGENT, actor_id="cfo")
    assert {x.task_id for x in ts.overdue(date(2026, 11, 20))} == {dep.task_id, t.task_id}
    events = store.events(object_id=t.task_id)
    assert [(e.previous_state, e.new_state) for e in events] == [(None, "BACKLOG"), ("BACKLOG", "IN_PROGRESS")]
