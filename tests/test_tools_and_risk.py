from decimal import Decimal

import pytest

from command_center.agents import AgentRegistry
from command_center.common import RiskClass
from command_center.errors import ConfigurationError, UnknownToolError
from command_center.policy import OrganizationPolicy
from command_center.risk import RiskContext, classify_action, requires_approval, validate_risk_class
from command_center.tools import DEFAULT_TOOL_REGISTRY, ToolCategory

from .conftest import human

POLICY = OrganizationPolicy.load()
REG = AgentRegistry.load_dir()


def ctx(declared=None, prior=Decimal(0)):
    return RiskContext(POLICY, declared, DEFAULT_TOOL_REGISTRY, prior)


def test_canonical_tool_set_and_metadata():
    names = set(DEFAULT_TOOL_REGISTRY.names())
    expected = {
        "read_kpis", "read_tasks", "read_projects", "read_customers", "read_leads", "read_opportunities",
        "read_financial_summary", "read_vendors", "read_knowledge", "search_company_data",
        "create_task", "update_task_status", "create_output", "create_recommendation", "request_approval",
        "create_alert", "record_kpi_observation", "propose_change_request",
        "send_customer_message", "publish_marketing_content", "create_purchase_order", "modify_customer_record",
        "schedule_meeting",
        "initiate_payment", "sign_contract", "approve_loan", "terminate_employee", "modify_permissions",
        "irreversible_delete",
    }
    assert names == expected
    for t in DEFAULT_TOOL_REGISTRY.all():
        if t.category is ToolCategory.HIGH_RISK:
            assert t.default_risk_class is RiskClass.RED and t.requires_approval
        if t.category is ToolCategory.EXTERNAL:
            assert t.default_risk_class is RiskClass.AMBER and t.requires_approval
    for t in ("sign_contract", "approve_loan", "terminate_employee", "modify_permissions", "irreversible_delete"):
        assert DEFAULT_TOOL_REGISTRY.get(t).allowed_roles == frozenset()


def test_unknown_tool_raises():
    with pytest.raises(UnknownToolError):
        classify_action("wire_money", {}, ctx())


def test_purchase_order_escalates_by_amount():
    small = classify_action("create_purchase_order", {"amount": 2000, "currency": "INR"}, ctx())
    large = classify_action("create_purchase_order", {"amount": 2_000_000, "currency": "INR"}, ctx())
    assert small.final_class is RiskClass.AMBER
    assert large.final_class is RiskClass.RED and large.escalated_by_payload


@pytest.mark.parametrize("payload, why", [
    ({"currency": "INR"}, "no 'amount'"),
    ({"amount": 100, "currency": "USD"}, "currency"),
    ({"amount": "lots", "currency": "INR"}, "not numeric"),
    ({"amount": -5, "currency": "INR"}, "negative"),
])
def test_unassessable_monetary_payload_fails_closed_to_red(payload, why):
    a = classify_action("create_purchase_order", payload, ctx())
    assert a.final_class is RiskClass.RED
    assert any(why in r for r in a.reasons)


def test_engine_is_currency_neutral():
    raw = {"organization_id": "o", "name": "n", "currency": "EUR", "timezone": "Europe/Berlin",
           "risk": {"monetary": {"create_purchase_order": {"amount_field": "total", "currency_field": "ccy",
                                                           "amber_at": 0, "red_at": 5000}}},
           "approvals": {"ttl_hours": {"GREEN": 24, "AMBER": 24, "RED": 4},
                         "max_ttl_hours": {"GREEN": 48, "AMBER": 48, "RED": 4},
                         "approver_roles": {"default": {"GREEN": "M", "AMBER": "M", "RED": "F"}}}}
    eur = OrganizationPolicy.from_mapping(raw)
    a = classify_action("create_purchase_order", {"total": 6000, "ccy": "EUR"}, RiskContext(eur))
    assert a.final_class is RiskClass.RED


def test_declared_lower_than_policy_is_overridden():
    a = classify_action("create_purchase_order", {"amount": 2_000_000, "currency": "INR"}, ctx(RiskClass.GREEN))
    assert a.final_class is RiskClass.RED and a.declared_lower_than_policy
    assert validate_risk_class(RiskClass.GREEN, a) == "declared GREEN is lower than policy RED"


def test_declared_higher_than_policy_is_retained():
    a = classify_action("read_kpis", {}, ctx(RiskClass.RED))
    assert a.policy_class is RiskClass.GREEN and a.final_class is RiskClass.RED
    assert a.declared_higher_than_policy


def test_bulk_message_and_sensitive_fields_are_red():
    bulk = classify_action("send_customer_message", {"recipients": [f"c{i}" for i in range(30)], "body": "x"}, ctx())
    one = classify_action("send_customer_message", {"recipients": ["c1"], "body": "x"}, ctx())
    assert bulk.final_class is RiskClass.RED and one.final_class is RiskClass.AMBER
    sens = classify_action("modify_customer_record", {"customer_id": "c", "changes": {"credit_limit": 1}}, ctx())
    plain = classify_action("modify_customer_record", {"customer_id": "c", "changes": {"phone": "1"}}, ctx())
    assert sens.final_class is RiskClass.RED and plain.final_class is RiskClass.AMBER


def test_split_purchase_protection():
    a = classify_action("create_purchase_order", {"amount": 600_000, "currency": "INR"}, ctx(prior=Decimal(600_000)))
    assert a.final_class is RiskClass.RED
    assert any("split-action" in r for r in a.reasons)


def test_requires_approval_rules():
    cos = REG.get_agent("chief_of_staff")
    green = classify_action("create_task", {"title": "x"}, ctx())
    can_auto = human("u", permissions={"auto_execute:GREEN"})
    cannot = human("u2")
    assert requires_approval(green, cos, POLICY, can_auto).required is False
    assert requires_approval(green, cos, POLICY, cannot).required is True
    red = classify_action("create_purchase_order", {"amount": 5_000_000, "currency": "INR"}, ctx())
    proc = REG.get_agent("procurement")
    everything = human("god", permissions={"auto_execute:GREEN", "auto_execute:AMBER"})
    d = requires_approval(red, proc, POLICY, everything)
    assert d.required and d.required_role == "FOUNDER"
    amber = classify_action("create_purchase_order", {"amount": 10, "currency": "INR"}, ctx())
    assert requires_approval(amber, proc, POLICY, everything).required_role == "FINANCE_APPROVER"


def test_agent_always_require_approval_list():
    cfo = REG.get_agent("cfo")
    a = classify_action("record_kpi_observation", {"kpi": "gross_margin"}, ctx())
    assert requires_approval(a, cfo, POLICY, human("u", permissions={"auto_execute:GREEN"})).required


def test_policy_cannot_auto_execute_red_tools():
    raw = {"organization_id": "o", "name": "n", "currency": "INR", "timezone": "UTC", "risk": {},
           "approvals": {"ttl_hours": {"GREEN": 1, "AMBER": 1, "RED": 1},
                         "max_ttl_hours": {"GREEN": 1, "AMBER": 1, "RED": 1},
                         "approver_roles": {"default": {"GREEN": "M", "AMBER": "M", "RED": "F"}}},
           "amber_auto_execute_tools": ["initiate_payment"]}
    with pytest.raises(ConfigurationError, match="never be auto-executed"):
        OrganizationPolicy.from_mapping(raw)


def test_policy_requires_bounded_red_ttl():
    raw = {"organization_id": "o", "name": "n", "currency": "INR", "timezone": "UTC", "risk": {},
           "approvals": {"ttl_hours": {"AMBER": 1}, "max_ttl_hours": {"AMBER": 1},
                         "approver_roles": {"default": {"GREEN": "M", "AMBER": "M", "RED": "F"}}}}
    with pytest.raises(ConfigurationError, match="RED"):
        OrganizationPolicy.from_mapping(raw)
