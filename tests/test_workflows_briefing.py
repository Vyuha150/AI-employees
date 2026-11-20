"""Parts 42-43 (daily brief, weekly review), 49-50 (workflow engine and safety)."""

import json
from datetime import date

import pytest

from command_center.agents import AgentRegistry
from command_center.audit import EventType
from command_center.briefing import BriefingService
from command_center.errors import PermissionDeniedError, PolicyViolationError, ValidationFailedError
from command_center.model_providers import mock_turn
from command_center.state_machines import TaskStatus
from command_center.workflows import StepType, WorkflowDefinition, WorkflowRunStatus, WorkflowStatus, WorkflowStep
from command_center.common import ActorType

from .conftest import ORG, human, load_raw_profiles
from .helpers import contract_responder, daily_brief, available_sources, runtime_cc

BRIEF_DAY = date(2026, 11, 20)


def test_daily_brief_end_to_end(clock, founder):
    cc, mock = runtime_cc(clock, responder=contract_responder())
    svc = BriefingService(cc, cc.runtime)
    res = svc.generate_daily_brief(founder, BRIEF_DAY)
    assert res.status == "GENERATED", res.error
    out = cc.outputs.get(res.output_id)
    assert out.kind.value == "DAILY_BRIEF" and out.validity.value == "VALID"
    assert out.content["data_quality"]["overall_status"] == res.data_quality["overall_status"]
    assert len(out.content["todays_priorities"]) <= 7
    assert set(res.invoked_agents) == {"data_analyst", "cfo", "sales_director", "operations", "customer_success",
                                       "marketing_director", "chief_of_staff"}
    # the Chief of Staff saw specialist summaries as citeable sources
    cos_call = [c for c in mock.calls if "Daily brief synthesis" in c["input"] or "DAILY_BRIEF" in c["input"]][-1]
    assert any(s["id"].startswith("out:") for s in available_sources(cos_call["input"]))
    # duplicate trigger: no new runs
    runs_before = len(cc.runs.repo.list())
    dup = svc.generate_daily_brief(founder, BRIEF_DAY)
    assert dup.status == "DUPLICATE" and dup.output_id == res.output_id
    assert len(cc.runs.repo.list()) == runs_before


def test_specialist_failure_marks_domain_unavailable_not_whole_brief(clock, founder):
    broken = {"sales_director": lambda s, last: mock_turn("I cannot produce JSON today")}
    cc, _ = runtime_cc(clock, responder=contract_responder(broken))
    res = BriefingService(cc, cc.runtime).generate_daily_brief(founder, BRIEF_DAY)
    assert res.status == "GENERATED"
    dq = res.data_quality
    assert dq["overall_status"] == "DEGRADED" and any("sales_director review" in m for m in dq["missing_sources"])
    assert cc.outputs.get(res.output_id).content["data_quality"]["missing_sources"] == dq["missing_sources"]
    assert any(a.category == "DATA_QUALITY" for a in cc.alerts.repo.list())


def test_brief_cannot_understate_data_quality(clock, founder):
    broken = {"sales_director": lambda s, last: mock_turn("nope")}

    def rosy_cos(session, last):
        first = session.messages[0]["text"]
        doc = daily_brief(first, available_sources(first))
        doc["data_quality"] = {"overall_status": "GOOD", "stale_sources": [], "missing_sources": [],
                               "conflicting_sources": [], "unverified_metrics": [], "notes": "all fine"}
        return mock_turn(json.dumps(doc))
    cc, _ = runtime_cc(clock, responder=contract_responder({**broken, "chief_of_staff": rosy_cos}))
    res = BriefingService(cc, cc.runtime).generate_daily_brief(founder, BRIEF_DAY)
    assert res.status == "FAILED" and "data quality" in res.error


def test_brief_priority_limit_enforced(clock, founder):
    def too_many(session, last):
        first = session.messages[0]["text"]
        return mock_turn(json.dumps(daily_brief(first, available_sources(first), priorities=9)))
    cc, _ = runtime_cc(clock, responder=contract_responder({"chief_of_staff": too_many}))
    res = BriefingService(cc, cc.runtime).generate_daily_brief(founder, BRIEF_DAY)
    assert res.status == "FAILED" and "todays_priorities" in res.error


def test_weekly_review_relevance_and_authority(clock, founder):
    cc, _ = runtime_cc(clock, responder=contract_responder())
    svc = BriefingService(cc, cc.runtime)
    week = date(2026, 11, 16)
    parts = svc.weekly_participants(founder, week, date(2026, 11, 22))
    assert "procurement" not in parts and "people_performance" not in parts and "product_analyst" not in parts
    res = svc.generate_weekly_review(founder, week)
    assert res.status == "GENERATED", res.error
    doc = cc.outputs.get(res.output_id).content
    assert doc["people"]["status"] == "UNAVAILABLE"
    for k in ("data_quality", "unresolved_items", "watchlist", "decisions_required", "priorities_next_week"):
        assert k in doc
    assert svc.generate_weekly_review(founder, week).status == "DUPLICATE"


def test_weekly_review_includes_procurement_when_relevant(clock, founder):
    cc, _ = runtime_cc(clock, responder=contract_responder())
    run = cc.runs.start(cc.runs.create_run("procurement", founder, "PO").run_id)
    cc.gateway.submit(run.run_id, {"tool": "create_purchase_order", "parameters": {"amount": 5, "currency": "INR"},
                                   "risk_class": "AMBER", "reason": "x"})
    parts = BriefingService(cc, cc.runtime).weekly_participants(founder, date(2026, 11, 16), date(2026, 11, 22))
    assert "procurement" in parts


def test_degraded_weekly_review_cannot_be_authoritative(clock, founder):
    broken = {"cfo": lambda s, last: mock_turn("x")}

    def claims_authority(session, last):
        from .helpers import weekly_review
        first = session.messages[0]["text"]
        return mock_turn(json.dumps(weekly_review(first, available_sources(first), authoritative=True)))
    cc, _ = runtime_cc(clock, responder=contract_responder({**broken, "chief_of_staff": claims_authority}))
    res = BriefingService(cc, cc.runtime).generate_weekly_review(founder, date(2026, 11, 16))
    assert res.status == "FAILED" and "authoritative" in res.error


# ---- workflow engine -----------------------------------------------------------------
def wf(steps, start="s1", wid=None):
    import uuid
    return WorkflowDefinition(wid or str(uuid.uuid4()), ORG, "test", 1, tuple(steps), start, "founder")


def activate(engine, d, founder):
    engine.create_definition(d)
    engine.set_status(d.workflow_id, WorkflowStatus.ACTIVE, founder)
    return d.workflow_id


def test_definitions_cannot_bypass_approval(clock, founder):
    cc, _ = runtime_cc(clock)
    engine = cc.workflow_engine()
    bad = wf([WorkflowStep("s1", StepType.TOOL, {"agent_id": "cfo", "tool": "initiate_payment",
                                                 "parameters": {"amount": 1, "currency": "INR"},
                                                 "skip_approval": True}, next="end"),
              WorkflowStep("end", StepType.END)])
    with pytest.raises(ValidationFailedError, match="cannot override approval"):
        engine.create_definition(bad)


def test_workflow_payment_still_requires_red_approval(clock, founder):
    raws = load_raw_profiles()
    raws["cfo"]["allowed_tools"].append("initiate_payment")       # explicitly granted for this test
    raws["cfo"]["prohibited_actions"].remove("initiate_payment")
    cc, _ = runtime_cc(clock, registry=AgentRegistry.from_mappings(raws))
    engine = cc.workflow_engine()
    d = wf([WorkflowStep("s1", StepType.TOOL, {"agent_id": "cfo", "tool": "initiate_payment",
                                               "parameters": {"amount": 1000, "currency": "INR", "payee": "v-1"}},
                         next="end"), WorkflowStep("end", StepType.END)])
    run = engine.start(activate(engine, d, founder), founder)
    assert run.status is WorkflowRunStatus.WAITING
    a = cc.approvals.get(run.waiting_on["approval_id"])
    assert a.risk_class.value == "RED" and a.required_role == "FOUNDER"
    assert engine.advance(run.workflow_run_id).status is WorkflowRunStatus.WAITING    # still pending
    cc.approve(a.approval_id, human("ceo", roles={"FOUNDER"}))
    done = engine.advance(run.workflow_run_id)
    assert done.status is WorkflowRunStatus.SUCCEEDED
    assert [c.tool_name for c in cc.external_executor.calls] == ["initiate_payment"]   # mocked, once


def test_denied_tool_fails_workflow(clock, founder):
    cc, _ = runtime_cc(clock)
    engine = cc.workflow_engine()
    d = wf([WorkflowStep("s1", StepType.TOOL, {"agent_id": "people_performance", "tool": "terminate_employee",
                                               "parameters": {"employee_id": "e1"}}, next="end"),
            WorkflowStep("end", StepType.END)])
    run = engine.start(activate(engine, d, founder), founder)
    assert run.status is WorkflowRunStatus.FAILED and "DENIED" in run.error


def test_gates_tasks_delays_conditions(clock, founder):
    cc, _ = runtime_cc(clock)
    engine = cc.workflow_engine()
    d = wf([
        WorkflowStep("s1", StepType.APPROVAL, {"required_role": "FOUNDER", "summary": "Start Q4 push?"}, next="s2"),
        WorkflowStep("s2", StepType.HUMAN_TASK, {"title": "Confirm targets", "owner_role": "MANAGER",
                                                 "completion_criteria": ["targets confirmed"]}, next="s3"),
        WorkflowStep("s3", StepType.DELAY, {"minutes": 30}, next="s4"),
        WorkflowStep("s4", StepType.CONDITION, {"ref": "steps.s2.status", "equals": "COMPLETED",
                                                "on_true": "s5", "on_false": "end"}),
        WorkflowStep("s5", StepType.NOTIFICATION, {"recipient": "human:FOUNDER", "subject": "Go", "body": "ready"},
                     next="end"),
        WorkflowStep("end", StepType.END)])
    with pytest.raises(PolicyViolationError):
        engine.start(engine.create_definition(d).workflow_id, founder)          # DRAFT cannot run
    engine.set_status(d.workflow_id, WorkflowStatus.ACTIVE, founder)
    run = engine.start(d.workflow_id, founder)
    assert run.waiting_on["type"] == "gate"
    with pytest.raises(PermissionDeniedError):
        engine.approve_gate(run.workflow_run_id, founder)                       # initiator cannot approve
    with pytest.raises(PermissionDeniedError):
        engine.approve_gate(run.workflow_run_id, human("mgr", roles={"MANAGER"}))
    run = engine.approve_gate(run.workflow_run_id, human("ceo", roles={"FOUNDER"}))
    assert run.waiting_on["type"] == "task"
    task_id = run.waiting_on["task_id"]
    assert engine.advance(run.workflow_run_id).status is WorkflowRunStatus.WAITING
    for s in (TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED):
        cc.tasks.transition(task_id, s, actor_type=ActorType.HUMAN, actor_id="mgr")
    run = engine.advance(run.workflow_run_id)
    assert run.waiting_on["type"] == "delay"
    clock.advance(minutes=31)
    run = engine.advance(run.workflow_run_id)
    assert run.status is WorkflowRunStatus.SUCCEEDED and "s5" in run.state["steps"]
    assert cc.audit.store.events(event_type=EventType.WORKFLOW_COMPLETED)


def test_kpi_update_step_is_governed(clock, founder):
    cc, _ = runtime_cc(clock)
    engine = cc.workflow_engine()
    d = wf([WorkflowStep("s1", StepType.KPI_UPDATE, {"agent_id": "cfo", "kpi": "gross_margin", "value": "41",
                                                     "unit": "percent", "observed_at": "2026-10-31",
                                                     "source_reference": "erp:pl"}, next="end"),
            WorkflowStep("end", StepType.END)])
    run = engine.start(activate(engine, d, founder), founder)
    assert run.status is WorkflowRunStatus.WAITING       # CFO profile: always_require_approval
    assert cc.kpis.list_kpis(ORG) == []


def test_agent_step_runs_through_runtime(clock, founder):
    cc, _ = runtime_cc(clock, responder=contract_responder())
    engine = cc.workflow_engine()
    d = wf([WorkflowStep("s1", StepType.AGENT, {"agent_id": "operations", "objective": "Review delivery risks"},
                         next="end"), WorkflowStep("end", StepType.END)])
    run = engine.start(activate(engine, d, founder), founder)
    assert run.status is WorkflowRunStatus.SUCCEEDED and run.state["steps"]["s1"]["output_id"]


def test_scheduler_dispatches_real_workflows(founder):
    from datetime import datetime, timezone
    from command_center.briefing import ScheduledWorkflowDispatcher
    from command_center.scheduler import ScheduledRunStatus
    from .conftest import FakeClock
    clock = FakeClock(datetime(2026, 11, 23, 0, 20, tzinfo=timezone.utc))     # Mon 05:50 IST
    cc, _ = runtime_cc(clock, responder=contract_responder())
    sched = cc.scheduler(ScheduledWorkflowDispatcher(cc, cc.runtime, founder))
    sched.sync_jobs()
    results = {}
    for minutes in (15, 30, 60):                                               # ticks at 06:05, 06:35, 07:35 IST
        clock.advance(minutes=minutes)
        results.update({r.job_id: r for r in sched.tick()})
    for job in ("daily_data_validation", "daily_finance_review", "daily_sales_review", "daily_brief"):
        assert results[job].status is ScheduledRunStatus.TRIGGERED, (job, results[job].reason)
    brief = cc.outputs.get(results["daily_brief"].workflow_ref)
    assert brief.kind.value == "DAILY_BRIEF" and brief.content["brief_date"] == "2026-11-23"
    clock.advance(minutes=5)
    assert not [r for r in sched.tick() if r.status is ScheduledRunStatus.TRIGGERED]
