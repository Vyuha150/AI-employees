"""Parts 35, 39, 54, 55: tool result contract, idempotency, retries, execution ambiguity."""

import pytest

from command_center.approvals import ExecutionStatus
from command_center.audit import EventType
from command_center.common import RiskClass
from command_center.errors import ApprovalError, ApprovalNotExecutableError, PermissionDeniedError
from command_center.executors import MockExternalExecutor, ToolInvocation, ToolResult, ToolStatus
from command_center.gateway import ActionStatus
from command_center.idempotency import InMemoryIdempotencyStore, idempotency_key
from command_center.retries import write_retry_allowed
from command_center.state_machines import AlertStatus, ApprovalStatus
from command_center.system import CommandCenter

from .conftest import action, human, running_run

PO = {"vendor_id": "v-1", "amount": 5000, "currency": "INR"}


def test_tool_result_contract():
    r = ToolResult(True, "ref", {"x": 1})
    assert r.status is ToolStatus.OK and not r.mock
    with pytest.raises(ValueError):
        ToolResult(True, status=ToolStatus.ERROR)
    with pytest.raises(ValueError):
        ToolResult(False, status=ToolStatus.OK)
    stale = ToolResult(True, status=ToolStatus.STALE, freshness="2026-01-01")
    assert stale.success
    mocked = ToolResult(True, "mock:1", {"mock": True}).for_model()
    assert mocked["mock"] and "SIMULATED RESULT" in mocked["notice"]
    assert set(ToolResult(False, error="e").for_model()) >= {"tool_call_id", "tool_name", "status", "data", "source_ids",
                                                            "warnings", "error", "executed_at", "mock"}


def test_gateway_results_carry_contract_fields(cc, founder):
    run = running_run(cc, "cfo", founder)
    out = cc.gateway.submit(run.run_id, action("read_financial_summary", {}, action_id="call-9"))
    r = out.result
    assert r.status is ToolStatus.UNAVAILABLE and r.tool_name == "read_financial_summary"
    assert r.tool_call_id == "call-9" and r.executed_at is not None and r.limitations


def approved_po(cc, founder, finance_approver):
    run = running_run(cc, "procurement", founder)
    o = cc.gateway.submit(run.run_id, action("create_purchase_order", PO, risk="AMBER"))
    cc.approve(o.approval_id, finance_approver)
    return run, o.approval_id


def test_timeout_is_execution_unknown_never_failure_or_retry(clock, founder, finance_approver):
    ext = MockExternalExecutor(timeout_tools={"create_purchase_order"})
    cc = CommandCenter.build(clock=clock, external_executor=ext, sleep=lambda s: None)
    run, aid = approved_po(cc, founder, finance_approver)
    res = cc.execute_approved(aid, tool_name="create_purchase_order", action_type="create_purchase_order", payload=PO)
    assert res.status is ActionStatus.EXECUTION_UNKNOWN and res.result.status is ToolStatus.EXECUTION_UNKNOWN
    a = cc.approvals.get(aid)
    assert a.status is ApprovalStatus.EXECUTION_UNKNOWN and a.execution_status is ExecutionStatus.UNKNOWN
    assert len(ext.calls) == 1                                      # not retried
    with pytest.raises(ApprovalNotExecutableError):
        cc.execute_approved(aid, tool_name="create_purchase_order", action_type="create_purchase_order", payload=PO)
    assert len(ext.calls) == 1
    alerts = cc.alerts.list("org_example", status=[AlertStatus.OPEN])
    assert any(al.severity.value == "CRITICAL" and "unknown" in al.title.lower() for al in alerts)
    assert any(t.owner == "human:FOUNDER" for t in cc.tasks.repo.list())
    assert cc.audit.store.events(event_type=EventType.TOOL_EXECUTION_UNKNOWN)

    with pytest.raises(PermissionDeniedError):           # requires the approval's role
        cc.approvals.reconcile(aid, human("someone", roles={"MANAGER"}), executed=True, evidence_ref="x", note="y")
    with pytest.raises(ApprovalError):
        cc.approvals.reconcile(aid, finance_approver, executed=True, evidence_ref="", note="")
    done = cc.approvals.reconcile(aid, finance_approver, executed=True, evidence_ref="supplier-portal:PO-778",
                                  note="Confirmed in supplier portal")
    assert done.status is ApprovalStatus.EXECUTED
    assert cc.audit.store.events(event_type=EventType.APPROVAL_RECONCILED)


def test_explicit_failure_is_not_ambiguous(clock, founder, finance_approver):
    ext = MockExternalExecutor(fail_tools={"create_purchase_order"})
    cc = CommandCenter.build(clock=clock, external_executor=ext, sleep=lambda s: None)
    _, aid = approved_po(cc, founder, finance_approver)
    res = cc.execute_approved(aid, tool_name="create_purchase_order", action_type="create_purchase_order", payload=PO)
    assert res.status is ActionStatus.FAILED and cc.approvals.get(aid).status is ApprovalStatus.FAILED
    assert len(ext.calls) == 1                                       # non-idempotent write: no retry


def test_red_never_retried_after_ambiguity():
    assert not write_retry_allowed(idempotent=True, risk=RiskClass.RED, ambiguous=True).allowed
    assert not write_retry_allowed(idempotent=True, risk=RiskClass.AMBER, ambiguous=True).allowed
    assert not write_retry_allowed(idempotent=False, risk=RiskClass.GREEN, ambiguous=False).allowed
    assert write_retry_allowed(idempotent=True, risk=RiskClass.AMBER, ambiguous=False).allowed


class Flaky:
    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    def fetch(self, inv):
        self.calls += 1
        if self.calls <= self.failures:
            return ToolResult(False, error="transient", status=ToolStatus.ERROR)
        return ToolResult(True, "r", {"records": []}, source_ids=("db:x:1",))


def test_read_tools_retry_per_policy(clock, founder):
    flaky = Flaky(1)
    cc = CommandCenter.build(clock=clock, read_connectors={"read_vendors": flaky}, sleep=lambda s: None)
    run = running_run(cc, "procurement", founder)
    out = cc.gateway.submit(run.run_id, action("read_vendors", {}))
    assert out.status is ActionStatus.EXECUTED and flaky.calls == 2
    assert cc.audit.store.events(event_type=EventType.TOOL_RETRIED)
    always = Flaky(10)
    cc2 = CommandCenter.build(clock=clock, read_connectors={"read_vendors": always}, sleep=lambda s: None)
    run2 = running_run(cc2, "procurement", founder)
    assert cc2.gateway.submit(run2.run_id, action("read_vendors", {})).status is ActionStatus.FAILED
    assert always.calls == cc2.policy.retries.read_tool.max_attempts


def test_execution_id_dedupes_external_calls():
    ext = MockExternalExecutor()
    inv = ToolInvocation("o", "a", "r", "create_purchase_order", "create_purchase_order", {}, execution_id="E1")
    first, second = ext.execute(inv), ext.execute(inv)
    assert first is second and len(ext.calls) == 1


def test_approved_executions_use_the_approval_as_execution_id(cc, founder, finance_approver):
    _, aid = approved_po(cc, founder, finance_approver)
    cc.execute_approved(aid, tool_name="create_purchase_order", action_type="create_purchase_order", payload=PO)
    assert cc.external_executor.calls[0].execution_id == aid


def test_idempotency_store():
    store = InMemoryIdempotencyStore()
    k = idempotency_key("org", "daily_brief", "2026-11-20")
    assert k == idempotency_key("org", "daily_brief", "2026-11-20") != idempotency_key("org2", "daily_brief", "2026-11-20")
    assert store.claim("org", k)[0] is True
    assert store.claim("org", k)[0] is False
    with pytest.raises(ValueError):
        idempotency_key("org", "kind")
