import copy

import pytest

from command_center.agents import CORE_ROSTER, AgentRegistry, RunContext, build_prompt_profile, validate_agent_profile
from command_center.common import AgentRole
from command_center.errors import (
    AgentDisabledError,
    AgentProfileMissingError,
    InvalidAgentProfileError,
    UnknownAgentError,
)
from command_center.prompt import build_system_prompt
from command_center.tools import DEFAULT_TOOL_REGISTRY

from .conftest import human


def test_all_twelve_core_profiles_are_valid():
    reg = AgentRegistry.load_dir()
    assert reg.validation_report() == {}
    reg.assert_all_valid()
    assert sorted(p.agent_id for p in reg.list_agents()) == sorted(CORE_ROSTER)
    assert {p.role for p in reg.list_agents()} == set(AgentRole)  # one agent per role


def test_hierarchy():
    reg = AgentRegistry.load_dir()
    assert reg.get_reports_to("chief_of_staff") is None  # human management
    assert reg.get_reports_to("lead_generation").agent_id == "sales_director"
    assert reg.get_reports_to("procurement").agent_id == "operations"
    direct = {p.agent_id for p in reg.get_direct_reports("chief_of_staff")}
    assert {"sales_director", "cfo", "operations", "marketing_director", "data_analyst"} <= direct
    assert "lead_generation" not in direct and "procurement" not in direct
    assert [p.agent_id for p in reg.get_direct_reports("sales_director")] == ["lead_generation"]
    assert "Finance" in reg.get_agents_by_department()


def test_profiles_are_distinct_and_complete(raw_profiles):
    missions = [p["mission"] for p in raw_profiles.values()]
    assert len(set(missions)) == 12
    for aid, p in raw_profiles.items():
        assert p["kpis"] and p["responsibilities"] and p["objectives"], aid
        for t in ("initiate_payment", "sign_contract", "terminate_employee", "modify_permissions"):
            assert t not in p["allowed_tools"], f"{aid} must not hold {t}"


def test_only_chief_of_staff_delegates():
    reg = AgentRegistry.load_dir()
    for p in reg.list_agents():
        assert p.delegation.can_delegate is (p.role is AgentRole.CHIEF_OF_STAFF)


def test_unknown_agent_fails_closed():
    with pytest.raises(UnknownAgentError):
        AgentRegistry.load_dir().get_agent("generic_helper")


def test_missing_profile_fails_closed(raw_profiles):
    del raw_profiles["cfo"]
    reg = AgentRegistry.from_mappings(raw_profiles)
    with pytest.raises(AgentProfileMissingError):
        reg.get_agent("cfo")
    assert "cfo" in reg.validation_report()


def test_disabled_agent_cannot_execute(raw_profiles):
    raw_profiles["marketing_director"]["enabled"] = False
    reg = AgentRegistry.from_mappings(raw_profiles)
    with pytest.raises(AgentDisabledError):
        reg.get_agent("marketing_director")
    assert reg.get_profile("marketing_director").enabled is False  # visible for reporting only
    assert "marketing_director" not in {p.agent_id for p in reg.list_agents()}


@pytest.mark.parametrize("mutate, expected", [
    (lambda p: p["allowed_tools"].append("initiate_payment"), "may not be granted"),
    (lambda p: p["allowed_tools"].append("teleport"), "unknown tool"),
    (lambda p: p.pop("mission"), "missing field 'mission'"),
    (lambda p: p.update(superuser=True), "unknown fields"),
    (lambda p: p["delegation"].update(can_delegate=True, allowed_targets=["cfo"]), "only the Chief of Staff"),
    (lambda p: p["prohibited_actions"].append("read_kpis"), "both allowed and prohibited"),
    (lambda p: p.update(autonomy_level="UNLIMITED"), "invalid autonomy_level"),
    (lambda p: p["cost_limits"].update(max_cost_per_run=0), "must be a positive number"),
])
def test_invalid_profile_cannot_execute(raw_profiles, mutate, expected):
    mutate(raw_profiles["sales_director"])
    reg = AgentRegistry.from_mappings(raw_profiles)
    with pytest.raises(InvalidAgentProfileError, match=expected):
        reg.get_agent("sales_director")


def test_reports_to_unknown_agent_invalidates(raw_profiles):
    raw_profiles["procurement"]["reports_to"] = "ghost"
    with pytest.raises(InvalidAgentProfileError, match="reports_to"):
        AgentRegistry.from_mappings(raw_profiles).get_agent("procurement")


def test_reporting_cycle_invalidates(raw_profiles):
    raw_profiles["chief_of_staff"]["reports_to"] = "cfo"
    reg = AgentRegistry.from_mappings(raw_profiles)
    with pytest.raises(InvalidAgentProfileError, match="cycle"):
        reg.get_agent("cfo")


def test_agent_id_must_match_file(raw_profiles):
    errs = validate_agent_profile(raw_profiles["cfo"], DEFAULT_TOOL_REGISTRY, expected_agent_id="finance")
    assert any("does not match file name" in e for e in errs)


def test_every_agent_builds_a_prompt_with_runtime_context(founder):
    reg = AgentRegistry.load_dir()
    for p in reg.list_agents():
        ctx = RunContext("Example org", (), (), "Do the job", founder)
        prompt = build_system_prompt(build_prompt_profile(p, ctx))
        assert f"AGENT_ID: {p.agent_id}" in prompt
        assert "CURRENT_USER: founder" in prompt
        assert "RESPONSIBILITIES:" in prompt and "PROHIBITED_ACTIONS:" in prompt


def test_runtime_permissions_come_only_from_principal():
    reg = AgentRegistry.load_dir()
    nobody = human("viewer")
    profile = build_prompt_profile(reg.get_agent("cfo"), RunContext("ctx", (), (), "task", nobody))
    assert profile["CURRENT_USER_PERMISSIONS"] == []


def test_original_example_profile_still_supported():
    # The Phase 1 example profile stays valid input for the prompt builder.
    import json
    from .conftest import ROOT
    profile = json.loads((ROOT / "profiles" / "chief_of_staff.example.json").read_text())
    assert "AGENT_ID: agent-chief-001" in build_system_prompt(copy.deepcopy(profile))
