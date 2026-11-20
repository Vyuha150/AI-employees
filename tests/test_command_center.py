import copy
import json
from pathlib import Path

import pytest

from command_center import REQUIRED_PROFILE_FIELDS, build_system_prompt, validate_response

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "profiles" / "chief_of_staff.example.json").read_text())
ALLOWED = PROFILE["ALLOWED_TOOLS"]


def base_response():
    return {
        "response_type": "RECOMMENDATION",
        "agent_id": "agent-chief-001",
        "executive_summary": "Overdue receivables from two accounts are the main near-term cash risk.",
        "facts": [{"text": "AR > 60 days is 18% of total AR.", "source_ids": ["s1"]}],
        "assumptions": [{"text": "No large inflows are expected this week."}],
        "findings": [
            {"text": "Late payments are concentrated in two accounts.", "kind": "INFERENCE",
             "cause_status": "SUSPECTED", "confidence": "MEDIUM", "source_ids": ["s1"]}
        ],
        "recommendations": [],
        "tasks": [],
        "proposed_actions": [
            {"action_id": "a1", "action_type": "create_task", "tool": "tasks.create_internal",
             "reason": "Assign collections follow-up", "parameters": {"owner": "finance"},
             "risk_class": "GREEN", "financial_impact": None, "customer_impact": "None",
             "reversible": True, "approval_required": False}
        ],
        "approval_requests": [],
        "alerts": [],
        "kpi_impacts": [],
        "memory_candidates": [],
        "confidence": "MEDIUM",
        "sources": [{"id": "s1", "type": "TOOL_RESULT", "reference": "finance.read:ar_aging"}],
    }


def test_prompt_injects_profile_and_keeps_all_sections():
    prompt = build_system_prompt(PROFILE)
    assert "{{AGENT_PROFILE}}" not in prompt
    assert "AGENT_ID: agent-chief-001" in prompt
    assert "<agent_profile>" in prompt
    for n in range(1, 31):
        assert f"\n{n}. " in prompt, f"section {n} missing"


def test_prompt_rejects_incomplete_or_unknown_profile():
    partial = {k: v for k, v in PROFILE.items() if k != "ALLOWED_TOOLS"}
    with pytest.raises(ValueError, match="ALLOWED_TOOLS"):
        build_system_prompt(partial)
    with pytest.raises(ValueError, match="unknown"):
        build_system_prompt({**PROFILE, "SUPERUSER": True})
    assert len(REQUIRED_PROFILE_FIELDS) == 17


def test_valid_response_passes():
    result = validate_response(base_response(), ALLOWED, expected_agent_id="agent-chief-001")
    assert result.ok, result.errors


def test_unknown_field_rejected_in_strict_mode():
    r = base_response()
    r["mood"] = "optimistic"
    assert not validate_response(r, ALLOWED).ok


def test_tool_not_allowed():
    r = base_response()
    r["proposed_actions"][0]["tool"] = "payments.send"
    result = validate_response(r, ALLOWED)
    assert any("not in ALLOWED_TOOLS" in e for e in result.errors)


def test_red_action_must_require_approval():
    r = base_response()
    r["proposed_actions"][0].update(risk_class="RED", approval_required=False)
    assert not validate_response(r, ALLOWED).ok  # schema-level rule


def test_amber_action_needs_matching_approval_request():
    r = base_response()
    r["proposed_actions"][0].update(risk_class="AMBER", approval_required=True)
    result = validate_response(r, ALLOWED)
    assert any("no approval_request" in e for e in result.errors)

    r["approval_requests"] = [{"action_id": "a1", "approver_role": "CFO",
                               "summary": "Approve outreach", "risk_class": "AMBER"}]
    assert validate_response(r, ALLOWED).ok


def test_fact_without_source_rejected():
    r = base_response()
    r["facts"][0]["source_ids"] = []
    assert not validate_response(r, ALLOWED).ok
    r = base_response()
    r["facts"][0]["source_ids"] = ["missing"]
    assert not validate_response(r, ALLOWED).ok


def test_memory_candidate_requires_source():
    r = base_response()
    r["memory_candidates"] = [{"content": "Net-30 is the approved default term.",
                               "category": "BUSINESS_RULE", "source_ids": [], "rationale": "policy"}]
    assert not validate_response(r, ALLOWED).ok


def test_full_recommendation_shape():
    r = base_response()
    rec = {
        "problem_or_opportunity": "Two accounts are 60+ days overdue.",
        "evidence": ["AR aging report"],
        "recommendation": "CFO calls both accounts this week and offers a payment plan.",
        "why_now": "Payroll is in 10 days.",
        "expected_impact": "Recover overdue cash.",
        "impact_category": "CASHFLOW",
        "estimated_value": None,
        "estimated_cost": None,
        "effort": "LOW", "urgency": "HIGH",
        "dependencies": [], "risks": ["Relationship strain"],
        "reversible": True, "confidence": "MEDIUM",
        "suggested_owner": "CFO", "kpis_affected": ["DSO"],
    }
    r["recommendations"] = [rec]
    assert validate_response(r, ALLOWED).ok
    bad = copy.deepcopy(r)
    del bad["recommendations"][0]["why_now"]
    assert not validate_response(bad, ALLOWED).ok
