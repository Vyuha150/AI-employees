from datetime import timedelta
from decimal import Decimal

import pytest

from command_center.approvals import ApprovalEngine, ExecutionStatus
from command_center.audit import AuditLogger, EventType, InMemoryAuditStore
from command_center.canonical import action_binding_hash, canonical_json
from command_center.common import ActorType, Principal, RiskClass
from command_center.errors import (
    ApprovalError,
    ApprovalNotExecutableError,
    ConfigurationError,
    PayloadMismatchError,
    PermissionDeniedError,
)
from command_center.policy import OrganizationPolicy
from command_center.state_machines import ApprovalStatus

from .conftest import ORG, FakeClock, human

PO = {"vendor_id": "v-17", "amount": 10_000, "currency": "INR", "items": [{"sku": "A", "qty": 10}]}


@pytest.fixture
def env():
    clock = FakeClock()
    store = InMemoryAuditStore()
    eng = ApprovalEngine(OrganizationPolicy.load(), AuditLogger(store, clock), clock=clock)
    return eng, clock, store


def request(eng, payload=PO, risk=RiskClass.AMBER, **kw):
    return eng.request_approval(organization_id=ORG, run_id="run_1", agent_id="procurement",
                                tool_name="create_purchase_order", action_type="create_purchase_order",
                                payload=payload, risk_class=risk, reason="restock", reversible=False,
                                requested_by="procurement", **kw)


def binding(payload=PO, **over):
    b = dict(organization_id=ORG, agent_id="procurement", run_id="run_1", tool_name="create_purchase_order",
             action_type="create_purchase_order", payload=payload)
    b.update(over)
    return b


FIN = human("fin", roles={"FINANCE_APPROVER"})
FOUNDER = human("founder", roles={"FOUNDER"})


# ---- payload integrity (Part 7) ----------------------------------------------------------
def test_canonical_hash_is_order_independent_and_type_sensitive():
    assert canonical_json({"b": 1, "a": [1, {"y": 2, "x": 1}]}) == canonical_json({"a": [1, {"x": 1, "y": 2}], "b": 1})
    h = lambda p: action_binding_hash(**binding(p))
    assert h({"amount": 10000}) != h({"amount": 10000.0})
    assert h({"amount": Decimal("10000.00")}) == h({"amount": Decimal("10000")})
    with pytest.raises(ValueError):
        h({"amount": float("nan")})


def test_approved_10k_cannot_execute_as_100k(env):
    eng, _, _ = env
    a = request(eng)
    eng.approve(a.approval_id, FIN)
    tampered = {**PO, "amount": 100_000}
    check = eng.can_execute(a.approval_id, **binding(tampered))
    assert not check.allowed and "differs" in check.reason
    with pytest.raises(PayloadMismatchError):
        eng.begin_execution(a.approval_id, **binding(tampered))
    # the untouched approval still works for the exact payload
    assert eng.can_execute(a.approval_id, **binding()).allowed


@pytest.mark.parametrize("over", [
    {"agent_id": "cfo"}, {"run_id": "run_2"}, {"tool_name": "initiate_payment"}, {"action_type": "other"},
    {"organization_id": "org_other"},
    {"payload": {**PO, "items": [{"sku": "A", "qty": 11}]}},
    {"payload": {**PO, "vendor_id": "v-18"}},
    {"payload": {**PO, "note": "added field"}},
])
def test_any_binding_change_invalidates(env, over):
    eng, _, _ = env
    a = request(eng)
    eng.approve(a.approval_id, FIN)
    assert not eng.can_execute(a.approval_id, **binding(**over)).allowed


def test_stored_payload_is_isolated_from_caller_mutation(env):
    eng, _, _ = env
    payload = {"amount": 10, "currency": "INR", "items": [1]}
    a = request(eng, payload)
    payload["items"].append(2)
    assert eng.get(a.approval_id).payload["items"] == [1]


# ---- approval rules (Part 6) -------------------------------------------------------------
def test_single_use(env):
    eng, _, _ = env
    a = request(eng)
    eng.approve(a.approval_id, FIN)
    eng.begin_execution(a.approval_id, **binding())
    with pytest.raises(ApprovalNotExecutableError, match="already been used"):
        eng.begin_execution(a.approval_id, **binding())
    done = eng.mark_executed(a.approval_id, "po-1")
    assert done.status is ApprovalStatus.EXECUTED and done.execution_status is ExecutionStatus.SUCCEEDED
    assert not eng.can_execute(a.approval_id, **binding()).allowed


def test_failed_execution_needs_new_approval(env):
    eng, _, _ = env
    a = request(eng)
    eng.approve(a.approval_id, FIN)
    eng.begin_execution(a.approval_id, **binding())
    failed = eng.mark_failed(a.approval_id, "supplier API down")
    assert failed.status is ApprovalStatus.FAILED
    assert not eng.can_execute(a.approval_id, **binding()).allowed


def test_approval_is_not_execution(env):
    eng, _, _ = env
    a = eng.approve(request(eng).approval_id, FIN)
    assert a.status is ApprovalStatus.APPROVED and a.execution_status is ExecutionStatus.NOT_STARTED
    with pytest.raises(ApprovalError):
        eng.mark_executed(a.approval_id)  # cannot record success without an execution


def test_expired_approval_cannot_execute(env):
    eng, clock, _ = env
    a = request(eng)
    eng.approve(a.approval_id, FIN)
    clock.advance(hours=73)
    assert not eng.can_execute(a.approval_id, **binding()).allowed
    with pytest.raises(ApprovalNotExecutableError):
        eng.begin_execution(a.approval_id, **binding())
    assert eng.get(a.approval_id).status is ApprovalStatus.EXPIRED


def test_cannot_approve_after_expiry(env):
    eng, clock, _ = env
    a = request(eng)
    clock.advance(hours=80)
    with pytest.raises(ApprovalError, match="EXPIRED"):
        eng.approve(a.approval_id, FIN)


def test_rejected_cannot_execute_or_be_approved(env):
    eng, _, _ = env
    a = request(eng)
    eng.reject(a.approval_id, FIN, "wrong vendor")
    assert not eng.can_execute(a.approval_id, **binding()).allowed
    with pytest.raises(ApprovalError):
        eng.approve(a.approval_id, FIN)


def test_red_approval_is_never_blanket(env):
    eng, _, _ = env
    a = request(eng, {**PO, "amount": 5_000_000}, RiskClass.RED)
    assert a.expires_at - a.requested_at == timedelta(hours=24)
    with pytest.raises(ConfigurationError):
        request(eng, {**PO, "amount": 5_000_000}, RiskClass.RED, ttl=timedelta(days=365))
    assert a.required_role == "FOUNDER"
    with pytest.raises(PermissionDeniedError):
        eng.approve(a.approval_id, FIN)  # finance approver lacks FOUNDER
    eng.approve(a.approval_id, FOUNDER)


@pytest.mark.parametrize("who, match", [
    (Principal("procurement", ORG, ActorType.AGENT, frozenset({"FINANCE_APPROVER"})), "human"),
    (human("procurement", roles={"FINANCE_APPROVER"}), "requester"),
    (human("someone", roles={"MANAGER"}), "required role"),
    (human("fin2", roles={"FINANCE_APPROVER"}, org="org_other"), "organization"),
])
def test_who_may_approve(env, who, match):
    eng, _, _ = env
    a = request(eng)
    with pytest.raises(PermissionDeniedError, match=match):
        eng.approve(a.approval_id, who)


def test_caller_cannot_choose_a_weaker_approver(env):
    eng, _, _ = env
    with pytest.raises(PermissionDeniedError):
        request(eng, required_role="INTERN")


def test_approval_history_is_append_only_events(env):
    eng, _, store = env
    a = request(eng)
    eng.approve(a.approval_id, FIN)
    eng.begin_execution(a.approval_id, **binding())
    eng.mark_executed(a.approval_id, "po-1")
    types = [e.event_type for e in eng.history(a.approval_id)]
    assert types == [EventType.APPROVAL_REQUESTED, EventType.APPROVAL_APPROVED,
                     EventType.APPROVAL_EXECUTION_STARTED, EventType.APPROVAL_EXECUTED]
    store.verify_chain()


def test_expire_due_sweeps(env):
    eng, clock, _ = env
    a = request(eng)
    clock.advance(hours=100)
    assert [x.approval_id for x in eng.expire_due()] == [a.approval_id]
