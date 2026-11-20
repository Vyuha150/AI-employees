import pytest

from command_center.errors import BudgetExceededError, PermissionDeniedError, PolicyViolationError
from command_center.orchestration import DelegationBudget, RequiredOutput
from command_center.state_machines import DelegationStatus
from command_center.tasks import Priority

from .conftest import human, running_run

BUDGET = DelegationBudget(max_cost=0.5, max_tokens=50_000, max_runtime_seconds=120)


def delegate(cc, parent, target, budget=BUDGET):
    return cc.orchestration.create_delegation(parent_run_id=parent, target_agent_id=target,
                                              objective=f"{target}: explain margin decline", context_refs=["kpi:gm"],
                                              required_output=RequiredOutput.STRUCTURED_RESPONSE,
                                              priority=Priority.P1, budget=budget)


def test_chief_of_staff_delegates_with_depth_one(cc, founder):
    cos = running_run(cc, "chief_of_staff", founder)
    d = delegate(cc, cos.run_id, "cfo")
    child = cc.orchestration.start_delegated_run(d.delegation_id)
    assert child.depth == 1 and child.parent_run_id == cos.run_id
    assert cc.orchestration.repo.get(d.delegation_id).status is DelegationStatus.RUNNING
    cc.runs.start(child.run_id)
    # depth limit: the delegated run cannot delegate further (also blocked: CFO cannot delegate)
    with pytest.raises((PermissionDeniedError, PolicyViolationError)):
        delegate(cc, child.run_id, "data_analyst")


def test_specialists_cannot_delegate(cc, founder):
    sales = running_run(cc, "sales_director", founder)
    with pytest.raises(PermissionDeniedError, match="not permitted to delegate"):
        delegate(cc, sales.run_id, "lead_generation")


def test_max_agents_per_orchestration(cc, founder):
    cos = running_run(cc, "chief_of_staff", founder)
    for t in ("cfo", "sales_director", "operations", "marketing_director"):
        delegate(cc, cos.run_id, t, DelegationBudget(0.1, 10_000, 60))
    with pytest.raises(PolicyViolationError, match="agents"):
        delegate(cc, cos.run_id, "data_analyst", DelegationBudget(0.1, 10_000, 60))


def test_total_token_budget_includes_parent_usage(cc, founder):
    cos = running_run(cc, "chief_of_staff", founder)
    cc.runs.record_usage(cos.run_id, tokens=150_000, cost=0.5)
    each = DelegationBudget(max_cost=0.5, max_tokens=120_000, max_runtime_seconds=60)  # within per-agent limits
    delegate(cc, cos.run_id, "cfo", each)
    delegate(cc, cos.run_id, "sales_director", each)              # 150k + 240k = 390k
    with pytest.raises(BudgetExceededError, match="token budget"):  # +120k = 510k > 500k
        delegate(cc, cos.run_id, "operations", each)


def test_runtime_budget_enforced(cc, founder, clock):
    cos = running_run(cc, "chief_of_staff", founder)
    clock.advance(seconds=850)
    with pytest.raises(BudgetExceededError, match="runtime"):
        delegate(cc, cos.run_id, "cfo", DelegationBudget(0.1, 1000, 120))


def test_budget_cannot_exceed_target_limits(cc, founder):
    cos = running_run(cc, "chief_of_staff", founder)
    with pytest.raises(BudgetExceededError):
        delegate(cc, cos.run_id, "cfo", DelegationBudget(max_cost=5.0, max_tokens=10, max_runtime_seconds=10))


def test_delegated_run_requires_principal_permission(cc):
    limited = human("analyst", permissions={"run_agent:chief_of_staff"})
    cos = running_run(cc, "chief_of_staff", limited)
    d = delegate(cc, cos.run_id, "cfo")
    with pytest.raises(PermissionDeniedError, match="may not run agent 'cfo'"):
        cc.orchestration.start_delegated_run(d.delegation_id)


def test_child_runs_only_via_delegation(cc, founder):
    cos = running_run(cc, "chief_of_staff", founder)
    with pytest.raises(PermissionDeniedError):
        cc.runs.create_run("cfo", founder, "sneaky", parent_run_id=cos.run_id)
