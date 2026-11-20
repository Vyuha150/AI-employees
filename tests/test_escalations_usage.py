"""Parts 40-41 (escalations, alerts) and 51-52 (usage, budgets)."""

from dataclasses import replace
from decimal import Decimal

import pytest

from command_center.audit import EventType
from command_center.common import ActorType
from command_center.config import ModelPricing
from command_center.errors import BudgetExceededError, InvalidTransitionError, PermissionDeniedError
from command_center.policy import ExhaustionPolicy, OrganizationPolicy
from command_center.state_machines import AlertStatus, TaskStatus
from command_center.system import CommandCenter
from command_center.usage import TokenUsage, compute_cost

from .conftest import ORG, Principal, action, human, running_run

TASK = {"title": "Collect AR", "objective": "cash", "description": "d", "owner": "cfo", "priority": "P0",
        "expected_output": "e", "completion_criteria": ["c"], "risk_level": "GREEN", "due_date": "2026-11-18"}


def test_overdue_task_escalates_once_with_alert_task_notification(cc, clock):
    t = cc.tasks.create_from_contract(TASK, organization_id=ORG, created_by="cfo", actor_type=ActorType.AGENT)
    rep = cc.escalations.evaluate()
    esc = [e for e in rep.triggered if e.object_id == t.task_id]
    assert len(esc) == 1 and esc[0].alert.severity.value == "CRITICAL"
    assert esc[0].task_id and cc.tasks.get(esc[0].task_id).owner == "human:FOUNDER"
    assert any(n.recipient == "human:FOUNDER" for n in cc.alerts.notifications.list())
    assert cc.audit.store.events(event_type=EventType.ESCALATION_TRIGGERED, object_id=t.task_id)
    assert not [e for e in cc.escalations.evaluate().triggered if e.object_id == t.task_id]   # de-duplicated


def test_blocked_and_stale_approvals_escalate(cc, clock, founder):
    t = cc.tasks.create_from_contract({**TASK, "priority": "P2", "due_date": None}, organization_id=ORG,
                                      created_by="cfo", actor_type=ActorType.AGENT)
    cc.tasks.transition(t.task_id, TaskStatus.IN_PROGRESS, actor_type=ActorType.AGENT, actor_id="cfo")
    cc.tasks.transition(t.task_id, TaskStatus.BLOCKED, actor_type=ActorType.AGENT, actor_id="cfo", reason="no data")
    run = running_run(cc, "procurement", founder)
    o = cc.gateway.submit(run.run_id, action("create_purchase_order", {"amount": 10, "currency": "INR"}, risk="AMBER"))
    assert not cc.escalations.evaluate().triggered
    clock.advance(hours=25)
    rules = {e.rule_id for e in cc.escalations.evaluate().triggered}
    assert {"blocked_too_long", "approval_stale"} <= rules
    assert o.approval_id


def test_repeated_failures_escalate(cc, clock, founder):
    run = running_run(cc, "cfo", founder)
    for _ in range(3):
        cc.gateway.submit(run.run_id, action("create_alert", {"severity": "WARNING"}))   # missing fields -> fails
    trig = cc.escalations.evaluate().triggered
    assert any(e.rule_id == "tool_failures" for e in trig)


def test_kpi_threshold_needs_sourced_data(cc):
    rep = cc.escalations.evaluate()
    assert any("cash_runway_low" in n for n in rep.not_evaluable)
    cc.kpis.record({"kpi": "cash_runway", "value": "4.5", "unit": "months", "observed_at": "2026-11-19",
                    "source_reference": "erp:cash_2026_11"}, organization_id=ORG, recorded_by="cfo")
    trig = [e for e in cc.escalations.evaluate().triggered if e.rule_id == "cash_runway_low"]
    assert trig and trig[0].alert.source_ids == ("erp:cash_2026_11",)


def test_deal_stalled_from_signals_only(cc):
    rep = cc.escalations.evaluate()
    assert any("deal_stalled" in n for n in rep.not_evaluable)
    signals = [{"type": "opportunity", "id": "opp-1", "name": "Acme", "value": 900000, "days_since_activity": 20,
                "source_id": "db:opportunities:opp-1"},
               {"type": "opportunity", "id": "opp-2", "value": 900000, "days_since_activity": 20}]   # unsourced
    rep = cc.escalations.evaluate(signals=signals)
    assert [e.object_id for e in rep.triggered if e.rule_id == "deal_stalled"] == ["opp-1"]
    assert any("opp-2" in n for n in rep.not_evaluable)


def test_escalation_never_executes_corrective_actions(cc):
    cc.kpis.record({"kpi": "cash_runway", "value": "1", "unit": "months", "observed_at": "2026-11-19",
                    "source_reference": "erp:x"}, organization_id=ORG, recorded_by="cfo")
    cc.escalations.evaluate()
    assert cc.external_executor.calls == [] and cc.approvals.repo.list() == []


def test_alert_lifecycle_is_human_only(cc, founder):
    a = cc.alerts.create(organization_id=ORG, severity="WARNING", category="TEST", title="t", description="d")
    with pytest.raises(PermissionDeniedError):
        cc.alerts.acknowledge(a.alert_id, Principal("cfo", ORG, ActorType.AGENT))
    cc.alerts.acknowledge(a.alert_id, founder)
    with pytest.raises(Exception):
        cc.alerts.resolve(a.alert_id, founder, "")
    done = cc.alerts.resolve(a.alert_id, founder, "fixed")
    assert done.status is AlertStatus.RESOLVED and done.resolved_by == "founder"
    with pytest.raises(InvalidTransitionError):
        cc.alerts.acknowledge(a.alert_id, founder)
    assert cc.audit.store.events(event_type=EventType.ALERT_RESOLVED)


def test_agent_create_alert_creates_record(cc, founder):
    run = running_run(cc, "cfo", founder)
    out = cc.gateway.submit(run.run_id, action("create_alert", {"severity": "WATCH", "title": "AR ageing",
                                                                "detail": "rising"}))
    assert out.status.value == "EXECUTED"
    assert cc.alerts.repo.get(out.result.result_ref).source_agent == "cfo"


# ---- usage & budgets -------------------------------------------------------------------
PRICE = ModelPricing(4.0, 20.0, 0.2, 5.0, "USD", "2026-11-03")


def test_cost_from_configured_pricing_only():
    cost, why, cur = compute_cost(PRICE, TokenUsage(1_000_000, 100_000, 200_000, 0))
    assert cost == Decimal("6.040000") and cur == "USD"          # 4 + 2 + 0.04
    assert compute_cost(None, TokenUsage(10, 10))[0] is None
    no_cache = replace(PRICE, cache_read_per_mtok=None)
    cost, why, _ = compute_cost(no_cache, TokenUsage(10, 10, 5, 0))
    assert cost is None and "cache read" in why


def spend(cc, agent="cfo", cost_tokens=(2_000_000, 0), run_id="r1"):
    cc.usage.record(run_id=run_id, agent_id=agent, provider="anthropic", model="claude-opus-5-5",
                    usage=TokenUsage(*cost_tokens), tool_calls=0, pricing=PRICE)


def test_block_new_runs_when_agent_daily_budget_spent(cc, founder):
    for _ in range(2):
        spend(cc)                        # 2 x 8 USD = 16 >= cfo daily 10
    with pytest.raises(BudgetExceededError, match="daily"):
        cc.runs.create_run("cfo", founder, "more")
    assert cc.audit.store.events(event_type=EventType.BUDGET_EXCEEDED)
    assert cc.runs.create_run("sales_director", founder, "fine")      # other agents unaffected


def test_warn_policy_continues(clock, founder):
    pol = OrganizationPolicy.load()
    pol = replace(pol, budgets=replace(pol.budgets, exhaustion_policy=ExhaustionPolicy.WARN))
    cc = CommandCenter.build(clock=clock, policy=pol)
    spend(cc), spend(cc)
    assert cc.runs.create_run("cfo", founder, "continue")
    assert cc.audit.store.events(event_type=EventType.BUDGET_EXCEEDED)


def test_require_override_needs_authorized_human(clock, founder):
    pol = OrganizationPolicy.load()
    pol = replace(pol, budgets=replace(pol.budgets, exhaustion_policy=ExhaustionPolicy.REQUIRE_OVERRIDE))
    cc = CommandCenter.build(clock=clock, policy=pol)
    spend(cc), spend(cc)
    with pytest.raises(BudgetExceededError):
        cc.runs.create_run("cfo", founder, "x")
    with pytest.raises(PermissionDeniedError):
        cc.usage.grant_override(human("mgr", roles={"MANAGER"}), scope="cfo", reason="quarter close", hours=4)
    with pytest.raises(Exception):
        cc.usage.grant_override(founder, scope="cfo", reason="quarter close", hours=100)
    cc.usage.grant_override(founder, scope="cfo", reason="quarter close", hours=4)
    assert cc.runs.create_run("cfo", founder, "x")
    assert cc.audit.store.events(event_type=EventType.BUDGET_OVERRIDE_GRANTED)
    clock.advance(hours=5)
    with pytest.raises(BudgetExceededError):
        cc.runs.create_run("cfo", founder, "x")


def test_organization_budget(cc, founder):
    for i in range(7):                    # 7 x 8 = 56 >= org daily 50, spread over agents
        spend(cc, agent=["sales_director", "operations", "marketing_director", "procurement", "legal_risk",
                         "product_analyst", "data_analyst"][i], run_id=f"r{i}")
    with pytest.raises(BudgetExceededError, match="organization daily"):
        cc.runs.create_run("customer_success", founder, "x")


def test_unknown_cost_is_reported_not_treated_as_zero(cc, founder):
    cc.usage.record(run_id="r", agent_id="cfo", provider="x", model="unpriced", usage=TokenUsage(10, 10),
                    tool_calls=0, pricing=None)
    decision = cc.usage.check_can_start(cc.registry.get_agent("cfo"))
    assert any("unknown cost" in w for w in decision.warnings)
