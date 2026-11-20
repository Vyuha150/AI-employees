"""Part 20: weekly review contract (typed, validated, never authoritative on degraded data)."""

import copy

import pytest

from command_center.briefs import WeeklyReview, validate_daily_brief, validate_weekly_review
from command_center.errors import ValidationFailedError

SRC = [{"id": "s1", "type": "COMPANY_DATABASE", "reference": "erp:pl_week_47"}]


def section(status="GREEN", **kw):
    return {"status": status, "summary": "summary", "trend": "UP", "key_findings": ["f"], "risks": [],
            "recommended_actions": ["a"], "source_ids": ["s1"], **kw}


REC = {"problem_or_opportunity": "Churn rose in segment B.", "evidence": ["CRM"], "evidence_source_ids": ["s1"],
       "recommendation": "Call the 5 at-risk accounts.", "why_now": "Renewals in 3 weeks.",
       "expected_impact": "Retain revenue.", "impact_category": "REVENUE", "estimated_value": None,
       "estimated_cost": None, "effort": "LOW", "urgency": "HIGH", "dependencies": [], "risks": [],
       "reversible": True, "confidence": "MEDIUM", "suggested_owner": "customer_success",
       "kpis_affected": ["churn"]}


def review(**over):
    doc = {
        "review_period_start": "2026-11-16", "review_period_end": "2026-11-22",
        "generated_at": "2026-11-22T18:00:00+00:00", "generated_by_agent": "chief_of_staff",
        "organization_id": "org_example", "authoritative": True,
        "overall_health": {"status": "GREEN", "score": 78, "summary": "solid", "components": []},
        **{k: section() for k in ("revenue", "profit", "cash", "pipeline", "delivery", "customers", "costs",
                                 "people", "risk")},
        "top_wins": [{"title": "Closed deal X", "business_impact": "+revenue", "evidence": ["crm"], "source_ids": ["s1"]}],
        "top_failures": [{"title": "Late shipment", "business_impact": "SLA", "root_cause_status": "SUSPECTED",
                          "source_ids": []}],
        "root_causes": [{"problem": "Late shipment", "cause": "Vendor delay", "confidence": "MEDIUM",
                         "verified": True, "source_ids": ["s1"]}],
        "decisions_required": [{"decision": "Second supplier?", "why_required": "Single point of failure",
                                "deadline": "2026-11-29", "risk_if_delayed": "More delays",
                                "recommended_option": "Qualify v-22", "alternatives": ["Wait"], "source_ids": ["s1"]}],
        "priorities_next_week": [{"title": f"P{i}", "objective": "o", "owner": "operations", "priority": "P1",
                                  "expected_impact": "x", "deadline": None, "kpi_ids": ["on_time_delivery"],
                                  "source_ids": ["s1"]} for i in range(3)],
        "recommendations": [REC],
        "watchlist": [{"title": "FX", "reason": "INR volatility", "monitoring_metric": "fx_rate",
                       "trigger_condition": "> 2% weekly move", "owner": "cfo", "source_ids": []}],
        "unresolved_items": [{"title": "Contract renewal", "reason_unresolved": "awaiting legal",
                              "owner": "legal_risk", "next_action": "review draft", "due_date": "2026-11-27",
                              "source_ids": []}],
        "data_quality": {"overall_status": "GOOD", "stale_sources": [], "missing_sources": [],
                         "conflicting_sources": [], "unverified_metrics": [], "notes": ""},
        "sources": SRC,
    }
    doc.update(over)
    return doc


def test_valid_review_and_typed_roundtrip():
    doc = review()
    assert validate_weekly_review(doc).ok
    typed = WeeklyReview.from_dict(doc)
    assert typed.is_authoritative and typed.sections["cash"].status == "GREEN"
    assert typed.root_causes[0].verified and typed.priorities_next_week[0].kpi_ids == ("on_time_delivery",)
    again = typed.to_dict()
    assert validate_weekly_review(again).ok


def test_not_authoritative_when_data_stale_or_missing():
    stale = review(data_quality={"overall_status": "DEGRADED", "stale_sources": [{"source": "crm", "last_updated": None}],
                                 "missing_sources": [], "conflicting_sources": [], "unverified_metrics": [], "notes": ""})
    r = validate_weekly_review(stale)
    assert not r.ok and "marked authoritative" in r.errors[0]
    stale["authoritative"] = False
    r = validate_weekly_review(stale)
    assert r.ok and r.warnings
    assert not WeeklyReview.from_dict(stale).is_authoritative
    unavailable = review(cash=section("UNAVAILABLE"))
    assert not validate_weekly_review(unavailable).ok


@pytest.mark.parametrize("mutate, expect", [
    (lambda d: d.update(recommendations=[REC] * 6), "recommendations"),
    (lambda d: d["priorities_next_week"].extend(copy.deepcopy(d["priorities_next_week"]) * 2), "priorities_next_week"),
    (lambda d: d["root_causes"][0].update(source_ids=[]), "verified root cause"),
    (lambda d: d["top_wins"][0].update(source_ids=["ghost"]), "not listed in sources"),
    (lambda d: d.update(review_period_end="2026-11-01"), "before review_period_start"),
    (lambda d: d.pop("data_quality"), "data_quality"),
    (lambda d: d["revenue"].update(confidence="HIGH"), "Additional properties"),
])
def test_contract_violations(mutate, expect):
    doc = review()
    mutate(doc)
    r = validate_weekly_review(doc)
    assert not r.ok and any(expect in e for e in r.errors), r.errors


def test_recommendation_limit_is_configurable():
    doc = review(recommendations=[REC] * 3)
    assert not validate_weekly_review(doc, max_recommendations=2).ok


def test_five_plus_priorities_warns():
    doc = review()
    doc["priorities_next_week"] = doc["priorities_next_week"] * 2
    r = validate_weekly_review(doc)
    assert r.ok and any("prefer 5" in w for w in r.warnings)


def test_from_dict_rejects_invalid():
    with pytest.raises(ValidationFailedError):
        WeeklyReview.from_dict(review(authoritative=True, cash=section("PARTIAL")))


def test_daily_brief_data_quality_is_optional_but_required_for_generated_briefs():
    from tests.test_lifecycle import brief
    b = brief()
    assert validate_daily_brief(b).ok
    assert not validate_daily_brief(b, require_data_quality=True).ok
