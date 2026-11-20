"""Phase 4 Part 1 (uncertain external execution) and Part 7 (adapter contracts)."""

from datetime import timedelta

import pytest

from command_center.adapters import (
    AdapterExecutor,
    AdapterOutcome,
    AdapterResult,
    AdapterSetting,
    MockAdapter,
    adapter_settings_from_policy,
)
from command_center.approvals import ExecutionStatus, ReconciliationOutcome
from command_center.audit import EventType
from command_center.errors import ApprovalError, ApprovalNotExecutableError, PermissionDeniedError
from command_center.executors import ToolInvocation, ToolStatus
from command_center.gateway import ActionStatus
from command_center.state_machines import AlertStatus, ApprovalStatus
from command_center.system import CommandCenter

from .conftest import action, human, running_run

PO = {"vendor_id": "v-1", "amount": 5000, "currency": "INR"}


def adapter_cc(clock, *script, enabled=True):
    adapter = MockAdapter("create_purchase_order", script=list(script))
    settings = adapter_settings_from_policy({"create_purchase_order": {"enabled": enabled,
                                                                       "adapter": adapter.name}})
    cc = CommandCenter.build(clock=clock, external_executor=AdapterExecutor(settings, {adapter.tool_name: adapter}),
                             sleep=lambda s: None)
    return cc, adapter


def approved(cc, founder, finance_approver, payload=PO):
    run = running_run(cc, "procurement", founder)
    o = cc.gateway.submit(run.run_id, action("create_purchase_order", payload, risk="AMBER"))
    cc.approve(o.approval_id, finance_approver)
    return o.approval_id


def execute(cc, aid, payload=PO):
    return cc.execute_approved(aid, tool_name="create_purchase_order", action_type="create_purchase_order",
                               payload=payload)


def assert_outcome_unknown(cc, aid, adapter, calls=1):
    a = cc.approvals.get(aid)
    assert a.status is ApprovalStatus.OUTCOME_UNKNOWN is ApprovalStatus.EXECUTION_UNKNOWN
    assert a.execution_status is ExecutionStatus.UNKNOWN
    assert len(adapter.calls) == calls                       # never retried
    open_alerts = cc.alerts.list(cc.policy.organization_id, status=[AlertStatus.OPEN])
    assert any(al.severity.value == "CRITICAL" and al.related_object_id == aid for al in open_alerts)
    assert any(t.owner == "human:FOUNDER" and "unknown" in t.title.lower() for t in cc.tasks.repo.list())
    assert cc.audit.store.events(event_type=EventType.APPROVAL_EXECUTION_UNKNOWN, object_id=aid)
    with pytest.raises(ApprovalNotExecutableError):
        execute(cc, aid)                                     # original approval never reused
    assert len(adapter.calls) == calls


@pytest.mark.parametrize("scenario", [
    AdapterResult(AdapterOutcome.PENDING_CONFIRMATION, "prov-1", "accepted; webhook not received"),
    AdapterResult(AdapterOutcome.UNKNOWN, None, "provider status: PROCESSING?"),
    AdapterResult(AdapterOutcome.CONFIRMED, None, "ok but no reference"),
    {"status": "maybe"},                                      # malformed response
    ConnectionResetError("connection lost after request submission"),
    TimeoutError("read timeout"),
])
def test_indeterminate_provider_outcomes_are_outcome_unknown(clock, founder, finance_approver, scenario):
    cc, adapter = adapter_cc(clock, scenario)
    aid = approved(cc, founder, finance_approver)
    res = execute(cc, aid)
    assert res.status is ActionStatus.OUTCOME_UNKNOWN and res.result.status is ToolStatus.OUTCOME_UNKNOWN
    assert not res.result.success
    assert_outcome_unknown(cc, aid, adapter)


def test_provably_unsent_request_is_a_safe_failure(clock, founder, finance_approver):
    cc, adapter = adapter_cc(clock, ConnectionRefusedError("refused"))
    aid = approved(cc, founder, finance_approver)
    res = execute(cc, aid)
    assert res.status is ActionStatus.FAILED and cc.approvals.get(aid).status is ApprovalStatus.FAILED


def test_gateway_crash_after_dispatch_is_recovered_as_unknown(clock, founder, finance_approver):
    cc, adapter = adapter_cc(clock)
    aid = approved(cc, founder, finance_approver)
    cc.approvals.begin_execution(aid, organization_id="org_example", agent_id="procurement",
                                 run_id=cc.approvals.get(aid).run_id, tool_name="create_purchase_order",
                                 action_type="create_purchase_order", payload=PO)   # ... then the process dies
    assert cc.gateway.recover_stale_executions(timedelta(minutes=30)) == []        # not stale yet
    clock.advance(minutes=31)
    (rec,) = cc.gateway.recover_stale_executions(timedelta(minutes=30))
    assert rec.approval_id == aid
    assert_outcome_unknown(cc, aid, adapter, calls=0)


def test_reconciliation_outcomes_and_reapproval(clock, founder, finance_approver):
    cc, adapter = adapter_cc(clock, TimeoutError("timeout"))
    aid = approved(cc, founder, finance_approver)
    execute(cc, aid)
    with pytest.raises(PermissionDeniedError):
        cc.approvals.reconcile_outcome(aid, human("mgr", roles={"MANAGER"}), ReconciliationOutcome.UNRESOLVED,
                                       evidence_ref="x", note="y")
    with pytest.raises(ApprovalError):
        cc.approvals.reconcile_outcome(aid, finance_approver, ReconciliationOutcome.CONFIRMED_NOT_EXECUTED,
                                       evidence_ref="", note="")
    still = cc.approvals.reconcile_outcome(aid, finance_approver, ReconciliationOutcome.UNRESOLVED,
                                           evidence_ref="ticket:SUP-91", note="supplier investigating")
    assert still.status is ApprovalStatus.OUTCOME_UNKNOWN and still.reconciliation_outcome.value == "UNRESOLVED"
    with pytest.raises(ApprovalNotExecutableError):
        cc.gateway.request_reexecution(aid, founder)          # not confirmed yet
    done = cc.approvals.reconcile_outcome(aid, finance_approver, ReconciliationOutcome.CONFIRMED_NOT_EXECUTED,
                                          evidence_ref="supplier-portal:no-PO", note="no PO in supplier portal")
    assert done.status is ApprovalStatus.FAILED and done.execution_status is ExecutionStatus.FAILED
    with pytest.raises(ApprovalNotExecutableError):
        execute(cc, aid)                                       # never reused
    new = cc.gateway.request_reexecution(aid, founder)
    assert new.approval_id != aid and new.supersedes == aid and new.status is ApprovalStatus.PENDING
    assert new.risk_class.value == "AMBER" and new.payload_hash == cc.approvals.get(aid).payload_hash
    with pytest.raises(ApprovalNotExecutableError):
        cc.gateway.request_reexecution(aid, founder)          # only one replacement
    with pytest.raises(ApprovalNotExecutableError):
        execute(cc, new.approval_id)                           # the new approval needs its own decision
    cc.approve(new.approval_id, finance_approver)
    assert execute(cc, new.approval_id).status is ActionStatus.EXECUTED
    assert cc.audit.store.events(event_type=EventType.APPROVAL_REEXECUTION_REQUESTED)


def test_confirmed_executed_reconciliation(clock, founder, finance_approver):
    cc, _ = adapter_cc(clock, TimeoutError("timeout"))
    aid = approved(cc, founder, finance_approver)
    execute(cc, aid)
    a = cc.approvals.reconcile_outcome(aid, finance_approver, ReconciliationOutcome.CONFIRMED_EXECUTED,
                                       evidence_ref="supplier-portal:PO-778", note="PO visible in portal")
    assert a.status is ApprovalStatus.EXECUTED and a.execution_result_ref == "supplier-portal:PO-778"
    with pytest.raises(ApprovalNotExecutableError):
        cc.gateway.request_reexecution(aid, founder)


def test_unknown_amounts_still_count_toward_split_threshold(clock, founder, finance_approver):
    cc, _ = adapter_cc(clock, TimeoutError("timeout"))
    big = {"vendor_id": "v", "amount": 600_000, "currency": "INR"}
    aid = approved(cc, founder, finance_approver, big)
    assert execute(cc, aid, big).status is ActionStatus.OUTCOME_UNKNOWN
    run = running_run(cc, "procurement", founder)
    o = cc.gateway.submit(run.run_id, action("create_purchase_order", {"vendor_id": "v", "amount": 600_000,
                                                                       "currency": "INR"}, risk="AMBER"))
    assert o.assessment.final_class.value == "RED"


# ---- adapters ---------------------------------------------------------------------------
def inv(tool="create_purchase_order", payload=PO, execution_id="E1"):
    return ToolInvocation("org_example", "procurement", "r", tool, tool, payload, execution_id=execution_id)


def test_external_writes_disabled_by_default():
    settings = adapter_settings_from_policy(None)
    assert all(not s.enabled for s in settings.values()) and set(settings) == {
        "send_customer_message", "schedule_meeting", "modify_customer_record", "create_purchase_order",
        "publish_marketing_content"}
    adapter = MockAdapter("create_purchase_order")
    ex = AdapterExecutor(settings, {"create_purchase_order": adapter})
    r = ex.execute(inv())
    assert r.status is ToolStatus.UNAVAILABLE and adapter.calls == []
    assert ex.execute(inv("initiate_payment", {"amount": 1})).status is ToolStatus.UNAVAILABLE


def test_adapter_idempotency_validation_and_mock_flag():
    adapter = MockAdapter("create_purchase_order")
    ex = AdapterExecutor({"create_purchase_order": AdapterSetting("create_purchase_order", True, adapter.name)},
                         {"create_purchase_order": adapter})
    first, second = ex.execute(inv()), ex.execute(inv())
    assert first.success and first.mock and first.data["mock"] is True
    assert first.result_ref == second.result_ref and len(adapter.calls) == 1        # same execution id
    assert ex.execute(inv(execution_id="E2")).result_ref != first.result_ref
    bad = ex.execute(inv(payload={"vendor_id": "v"}, execution_id="E3"))
    assert bad.status is ToolStatus.ERROR and "not sent" in bad.error and len(adapter.calls) == 2
    assert ex.execute(inv(execution_id=None)).status is ToolStatus.ERROR
    status = ex.provider_status("create_purchase_order", "E1", first.result_ref)
    assert status.outcome is AdapterOutcome.CONFIRMED and status.mock


@pytest.mark.parametrize("tool, payload", [
    ("send_customer_message", {"recipients": ["c1"], "body": "hello"}),
    ("schedule_meeting", {"participants": ["a"], "start": "2026-11-23T10:00", "duration_minutes": 30}),
    ("modify_customer_record", {"customer_id": "c1", "changes": {"phone": "1"}}),
    ("publish_marketing_content", {"channel": "blog", "content": "post"}),
])
def test_every_adapter_contract(tool, payload):
    adapter = MockAdapter(tool)
    ex = AdapterExecutor({tool: AdapterSetting(tool, True, adapter.name)}, {tool: adapter})
    assert ex.execute(inv(tool, payload)).success
    assert ex.execute(inv(tool, {}, execution_id="E9")).status is ToolStatus.ERROR
