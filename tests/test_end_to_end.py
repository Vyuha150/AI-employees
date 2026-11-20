"""End-to-end flows through CommandCenter: run -> output -> validation -> ingestion -> risk ->
approval -> execution -> audit -> performance."""

from datetime import timedelta

import pytest

from command_center.agents import AgentRegistry, RunContext
from command_center.audit import EventType
from command_center.common import ActorType
from command_center.errors import (
    AgentDisabledError,
    ApprovalNotExecutableError,
    BudgetExceededError,
    InvalidTransitionError,
    PayloadMismatchError,
    PermissionDeniedError,
    PolicyViolationError,
)
from command_center.evaluation import OutputEvaluation, ReviewDecision
from command_center.executors import MockExternalExecutor
from command_center.gateway import ActionStatus
from command_center.performance import Period, compute_agent_performance
from command_center.state_machines import ApprovalStatus, RunStatus, TaskStatus
from command_center.system import CommandCenter

from .conftest import action, human, load_raw_profiles, running_run


def response(agent_id, *, tasks=(), actions=(), recs=(), memories=(), sources=None):
    return {
        "response_type": "ANALYSIS", "agent_id": agent_id, "executive_summary": "summary",
        "facts": [{"text": "Stock of SKU A is 12 units.", "source_ids": ["s1"]}], "assumptions": [],
        "findings": [], "recommendations": list(recs), "tasks": list(tasks), "proposed_actions": list(actions),
        "approval_requests": [{"action_id": a["action_id"], "approver_role": "FINANCE_APPROVER", "summary": "approve",
                               "risk_class": a["risk_class"]} for a in actions if a["approval_required"]],
        "alerts": [], "kpi_impacts": [], "memory_candidates": list(memories), "confidence": "MEDIUM",
        "sources": sources or [{"id": "s1", "type": "TOOL_RESULT", "reference": "read_vendors:v-17"}],
    }


TASK = {"title": "Confirm vendor delivery window", "objective": "Avoid stock-out", "description": "Call v-17",
        "owner": "procurement", "priority": "P1", "dependencies": [], "expected_output": "Confirmed date",
        "completion_criteria": ["Date confirmed in writing"], "kpi_affected": ["lead_time"], "risk_level": "GREEN"}


def po(amount, aid="po1"):
    return action("create_purchase_order", {"vendor_id": "v-17", "amount": amount, "currency": "INR",
                                            "items": [{"sku": "A", "qty": 100}]},
                  risk="AMBER", action_id=aid, reversible=False)


def ingest(cc, run, resp):
    out = cc.outputs.record(run, resp)
    assert out.validation_errors == (), out.validation_errors
    return out, cc.ingest_output(out.output_id)


# -------------------------------------------------------------------------------------------
def test_green_task_auto_executes_for_permitted_principal(cc, founder):
    run = running_run(cc, "procurement", founder)
    _, report = ingest(cc, run, response("procurement", tasks=[TASK]))
    (t,) = report.tasks
    assert t.status is ActionStatus.EXECUTED
    task = cc.tasks.get(t.result.result_ref)
    assert task.owner == "procurement" and task.status is TaskStatus.BACKLOG
    assert cc.runs.get(run.run_id).status is RunStatus.RUNNING
    assert cc.audit.store.events(event_type=EventType.TOOL_EXECUTED, object_id="create_task")


def test_green_task_needs_approval_without_auto_execute_permission(cc):
    p = human("analyst", permissions={"run_agent:procurement"})
    run = running_run(cc, "procurement", p)
    _, report = ingest(cc, run, response("procurement", tasks=[TASK]))
    (t,) = report.tasks
    assert t.status is ActionStatus.PENDING_APPROVAL
    assert cc.approvals.get(t.approval_id).required_role == "MANAGER"
    assert cc.runs.get(run.run_id).status is RunStatus.APPROVAL_REQUIRED


def test_purchase_order_amber_full_flow_with_tamper_attempt(cc, founder, finance_approver):
    run = running_run(cc, "procurement", founder)
    mislabeled = {**po(2000), "risk_class": "GREEN", "approval_required": False}
    resp = response("procurement", actions=[mislabeled])
    _, report = ingest(cc, run, resp)
    (outcome,) = report.actions
    assert outcome.status is ActionStatus.PENDING_APPROVAL
    assert outcome.assessment.final_class.value == "AMBER"
    assert cc.audit.store.events(event_type=EventType.RISK_OVERRIDDEN_BY_POLICY, run_id=run.run_id)
    assert cc.runs.get(run.run_id).status is RunStatus.APPROVAL_REQUIRED

    a = cc.approve(outcome.approval_id, finance_approver)
    assert a.status is ApprovalStatus.APPROVED
    ext: MockExternalExecutor = cc.external_executor
    assert ext.calls == []  # approval is not execution

    with pytest.raises(PayloadMismatchError):
        cc.execute_approved(outcome.approval_id, tool_name="create_purchase_order",
                            action_type="create_purchase_order",
                            payload={**mislabeled["parameters"], "amount": 20000})
    assert ext.calls == []

    done = cc.execute_approved(outcome.approval_id, tool_name="create_purchase_order",
                               action_type="create_purchase_order", payload=mislabeled["parameters"])
    assert done.status is ActionStatus.EXECUTED and done.result.data == {"mock": True}
    assert len(ext.calls) == 1 and ext.calls[0].payload["amount"] == 2000
    assert cc.approvals.get(outcome.approval_id).status is ApprovalStatus.EXECUTED
    assert cc.runs.get(run.run_id).status is RunStatus.RUNNING  # resumed
    with pytest.raises(ApprovalNotExecutableError):  # single use
        cc.execute_approved(outcome.approval_id, tool_name="create_purchase_order",
                            action_type="create_purchase_order", payload=mislabeled["parameters"])
    assert len(ext.calls) == 1


def test_large_purchase_order_is_red_and_needs_founder(cc, founder, finance_approver):
    run = running_run(cc, "procurement", founder)
    _, report = ingest(cc, run, response("procurement", actions=[po(2_000_000)]))
    (o,) = report.actions
    a = cc.approvals.get(o.approval_id)
    assert a.risk_class.value == "RED" and a.required_role == "FOUNDER"
    assert a.expires_at - a.requested_at == timedelta(hours=24)
    with pytest.raises(PermissionDeniedError):
        cc.approve(o.approval_id, finance_approver)
    cc.approve(o.approval_id, human("ceo", roles={"FOUNDER"}))


def test_split_purchase_orders_escalate(cc, founder):
    run = running_run(cc, "procurement", founder)
    _, report = ingest(cc, run, response("procurement", actions=[po(600_000, "p1"), po(600_000, "p2")]))
    first, second = report.actions
    assert first.assessment.final_class.value == "AMBER"
    assert second.assessment.final_class.value == "RED"


def test_output_with_unauthorized_tool_is_stored_but_not_ingested(cc, founder):
    run = running_run(cc, "marketing_director", founder)
    bad = response("marketing_director", actions=[po(100)])
    out = cc.outputs.record(run, bad)
    assert out.validity.value == "INVALID" and any("ALLOWED_TOOLS" in e for e in out.validation_errors)
    with pytest.raises(PolicyViolationError):
        cc.ingest_output(out.output_id)
    assert cc.audit.store.events(event_type=EventType.OUTPUT_CREATED, run_id=run.run_id)


def test_gateway_denies_tools_outside_profile_even_if_output_is_skipped(cc, founder):
    run = running_run(cc, "people_performance", founder)
    out = cc.gateway.submit(run.run_id, action("terminate_employee", {"employee_id": "e1"}, risk="RED"))
    assert out.status is ActionStatus.DENIED and "prohibited" in out.reason
    out = cc.gateway.submit(run.run_id, action("send_customer_message", {"recipients": ["c"]}, risk="AMBER"))
    assert out.status is ActionStatus.DENIED
    assert len(cc.audit.store.events(event_type=EventType.TOOL_DENIED, run_id=run.run_id)) == 2


def test_rejection_resumes_run_and_blocks_execution(cc, founder, finance_approver):
    run = running_run(cc, "procurement", founder)
    _, report = ingest(cc, run, response("procurement", actions=[po(5000)]))
    (o,) = report.actions
    cc.reject(o.approval_id, finance_approver, "use framework vendor instead")
    assert cc.runs.get(run.run_id).status is RunStatus.RUNNING
    with pytest.raises(ApprovalNotExecutableError, match="REJECTED"):
        cc.execute_approved(o.approval_id, tool_name="create_purchase_order", action_type="create_purchase_order",
                            payload=po(5000)["parameters"])
    assert cc.external_executor.calls == []


def test_execution_failure_is_reported_not_hidden(clock, founder, finance_approver):
    ext = MockExternalExecutor(fail_tools={"create_purchase_order"})
    cc = CommandCenter.build(clock=clock, external_executor=ext)
    run = running_run(cc, "procurement", founder)
    _, report = ingest(cc, run, response("procurement", actions=[po(5000)]))
    (o,) = report.actions
    cc.approve(o.approval_id, finance_approver)
    res = cc.execute_approved(o.approval_id, tool_name="create_purchase_order", action_type="create_purchase_order",
                              payload=po(5000)["parameters"])
    assert res.status is ActionStatus.FAILED
    assert cc.approvals.get(o.approval_id).status is ApprovalStatus.FAILED
    assert cc.audit.store.events(event_type=EventType.TOOL_FAILED, run_id=run.run_id)


def test_read_without_data_adapter_fails_honestly(cc, founder):
    run = running_run(cc, "cfo", founder)
    out = cc.gateway.submit(run.run_id, action("read_financial_summary", {"period": "2026-10"}))
    assert out.status is ActionStatus.FAILED and "data unavailable" in out.reason


def test_disabling_agent_blocks_execution_of_prior_approval(cc, founder, finance_approver):
    run = running_run(cc, "procurement", founder)
    _, report = ingest(cc, run, response("procurement", actions=[po(5000)]))
    (o,) = report.actions
    cc.approve(o.approval_id, finance_approver)
    raws = load_raw_profiles()
    raws["procurement"]["enabled"] = False
    cc.gateway.registry = AgentRegistry.from_mappings(raws)
    res = cc.execute_approved(o.approval_id, tool_name="create_purchase_order", action_type="create_purchase_order",
                              payload=po(5000)["parameters"])
    assert res.status is ActionStatus.DENIED
    assert cc.external_executor.calls == []
    with pytest.raises(AgentDisabledError):
        cc.gateway.registry.get_agent("procurement")


def test_run_budget_fails_run(cc, founder):
    run = running_run(cc, "lead_generation", founder)
    with pytest.raises(BudgetExceededError):
        cc.runs.record_usage(run.run_id, tokens=200_000, cost=0.1)
    assert cc.runs.get(run.run_id).status is RunStatus.FAILED


def test_full_cycle_feeds_performance_and_audit(cc, founder, clock):
    run = running_run(cc, "procurement", founder)
    prompt = cc.runs.build_system_prompt(run.run_id, RunContext("Example org", (), (), "Restock SKU A", founder))
    assert "AGENT_ID: procurement" in prompt
    rec = {"problem_or_opportunity": "Vendor v-17 lead time rose from 7 to 15 days.",
           "evidence": ["read_vendors v-17 history"], "evidence_source_ids": ["s1"],
           "recommendation": "Qualify v-22 as second source for SKU A.", "why_now": "Stock cover is 5 days.",
           "expected_impact": "Lower stock-out risk.", "impact_category": "OPERATIONAL_RELIABILITY",
           "estimated_value": None, "estimated_cost": None, "effort": "MEDIUM", "urgency": "HIGH",
           "dependencies": [], "risks": ["Qualification time"], "reversible": True, "confidence": "MEDIUM",
           "suggested_owner": "procurement", "kpis_affected": ["supplier_reliability"]}
    mem = {"content": "v-17 requires 15 days lead time.", "category": "VENDOR_CONSTRAINT", "source_ids": ["s1"],
           "rationale": "Affects reorder points", "scope": "DEPARTMENT"}
    out, report = ingest(cc, run, response("procurement", tasks=[TASK], recs=[rec], memories=[mem]))
    assert report.errors == ()
    assert report.recommendations[0].status.value == "PROPOSED"
    assert report.memories[0].status.value == "PROPOSED" and report.memories[0].scope_ref == "Operations"

    task_id = report.tasks[0].result.result_ref
    for s in (TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED):
        cc.tasks.transition(task_id, s, actor_type=ActorType.HUMAN, actor_id="founder")
    cc.outputs.review(OutputEvaluation(out.output_id, "founder", clock(), 4, 4, 4, 5, 4, ReviewDecision.APPROVED),
                      founder)
    cc.runs.record_usage(run.run_id, tokens=10_000, cost=0.25)
    cc.runs.transition(run.run_id, RunStatus.SUCCEEDED)
    with pytest.raises(InvalidTransitionError):
        cc.runs.transition(run.run_id, RunStatus.RUNNING)  # terminal

    perf = compute_agent_performance(cc.audit.store, "procurement", Period.D7, clock())
    assert perf.tasks_completed == 1 and perf.completion_rate == 1.0
    assert perf.output_acceptance_rate == 1.0 and perf.average_quality_score == pytest.approx(4.2)
    assert perf.run_failure_rate == 0.0 and perf.estimated_cost == 0.25 and perf.cost_per_completed_task == 0.25
    assert perf.tool_failure_rate == 0.0
    assert perf.business_outcomes_verified == 0

    cc.audit.store.verify_chain()
    kinds = {e.event_type for e in cc.audit.store.events(run_id=run.run_id)}
    assert {EventType.AGENT_RUN_CREATED, EventType.AGENT_RUN_STARTED, EventType.OUTPUT_CREATED,
            EventType.TASK_CREATED, EventType.RECOMMENDATION_CREATED, EventType.MEMORY_PROPOSED,
            EventType.AGENT_RUN_COMPLETED} <= kinds


def test_principal_cannot_run_agents_without_permission(cc):
    with pytest.raises(PermissionDeniedError):
        cc.runs.create_run("cfo", human("guest"), "look at cash")


def test_retry_is_a_new_run(cc, founder):
    run = running_run(cc, "cfo", founder)
    cc.runs.transition(run.run_id, RunStatus.FAILED, reason="tool outage")
    retry = cc.runs.create_run("cfo", founder, "retry", retry_of=run.run_id)
    assert retry.run_id != run.run_id and retry.retry_of == run.run_id
    assert cc.runs.get(run.run_id).status is RunStatus.FAILED


def test_kpi_observation_requires_source_and_approval_for_cfo(cc, founder):
    run = running_run(cc, "cfo", founder)
    o = cc.gateway.submit(run.run_id, action("record_kpi_observation", {
        "kpi": "gross_margin", "value": "41.2", "unit": "percent", "observed_at": "2026-10-31",
        "source_reference": "erp:pl_2026_10"}))
    assert o.status is ActionStatus.PENDING_APPROVAL  # CFO profile: always_require_approval
    da = running_run(cc, "data_analyst", founder)
    bad = cc.gateway.submit(da.run_id, action("record_kpi_observation", {"kpi": "x", "value": "1", "unit": "u",
                                                                          "observed_at": "2026-10-31"}))
    assert bad.status is ActionStatus.FAILED and "source" in bad.reason
    assert cc.internal_executor.kpi_observations == []
