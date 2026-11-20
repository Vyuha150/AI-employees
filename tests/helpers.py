"""Shared test helpers: mock model configuration and a contract-following mock responder."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from command_center.config import ModelsConfig
from command_center.model_providers import MockModelProvider, ModelSession, ProviderRegistry, mock_turn

PRICING = {"mock": {"mock-model": {"input_per_mtok": 1.0, "output_per_mtok": 2.0, "cache_read_per_mtok": 0.1,
                                   "cache_write_per_mtok": 1.25, "currency": "USD", "as_of": "test"}}}


def mock_models(*, fallback: bool = False, pricing: dict | None = None) -> ModelsConfig:
    raw = {"default_profile": "standard", "providers": {"mock": {}, "backup": {}},
           "model_profiles": {n: {"provider": "mock", "model": "mock-model", "max_tokens": 4000, "effort": "low"}
                              for n in ("standard", "deep")},
           "fallback": {"allowed": fallback, "chains": {"standard": [{"provider": "backup", "model": "backup-model"}],
                                                        "deep": [{"provider": "backup", "model": "backup-model"}]}
                        if fallback else {}},
           "pricing": PRICING if pricing is None else pricing}
    return ModelsConfig.from_mapping(raw)


def agent_of(session: ModelSession) -> str:
    return re.search(r"AGENT_ID: (\S+)", session.system).group(1)


def available_sources(text: str) -> list[dict]:
    m = re.search(r"AVAILABLE SOURCES[^\n]*\n(.*?)\n\nOUTPUT", text, re.S)
    return json.loads(m.group(1)) if m else []


def kind_of(text: str) -> str:
    m = re.search(r"STRUCTURED MODE \((\w+)\)", text)
    return m.group(1) if m else "STRUCTURED_RESPONSE"


def structured(agent: str, sources: list[dict], **over: Any) -> dict:
    src = sources[:1]
    resp = {"response_type": "ANALYSIS", "agent_id": agent, "executive_summary": f"{agent} summary",
            "facts": [{"text": "A sourced fact.", "source_ids": [src[0]["id"]]}] if src else [],
            "assumptions": [], "findings": [], "recommendations": [], "tasks": [], "proposed_actions": [],
            "approval_requests": [], "alerts": [], "kpi_impacts": [], "memory_candidates": [], "confidence": "MEDIUM",
            "sources": src}
    resp.update(over)
    return resp


def _section(status="GREEN", ids=()):
    return {"status": status, "summary": f"{status} summary", "trend": "FLAT", "key_findings": [], "risks": [],
            "recommended_actions": [], "source_ids": list(ids)}


def _dq_from(text: str) -> dict:
    m = re.search(r"never better\): (\{.*?\})\n", text + "\n", re.S)
    return json.loads(m.group(1)) if m else {"overall_status": "GOOD", "stale_sources": [], "missing_sources": [],
                                              "conflicting_sources": [], "unverified_metrics": [], "notes": ""}


def daily_brief(text: str, sources: list[dict], *, priorities: int = 3) -> dict:
    day = re.search(r"brief_date (\d{4}-\d{2}-\d{2})", text).group(1)
    org = re.search(r"organization_id (\S+?),", text).group(1)
    sec = {"summary": "ok", "items": [{"text": "noted", "kind": "INFERENCE"}]}
    return {"brief_date": day, "organization_id": org, "agent_id": "chief_of_staff",
            "executive_summary": "Synthesized brief.",
            "business_health": {"score": None, "components": [{"name": "cash", "status": "AMBER", "summary": "watch"}]},
            **{k: sec for k in ("revenue", "cash", "pipeline", "operations", "customers", "costs", "risk")},
            "todays_priorities": [{"title": f"Priority {i}", "why_it_matters": "impact", "owner": "founder",
                                   "source_agent": "cfo", "urgency": "HIGH", "expected_impact": "x", "deadline": None,
                                   "status": "NOT_STARTED"} for i in range(priorities)],
            "decisions_required": [], "ai_actions_in_progress": [], "watchlist": [],
            "data_quality": _dq_from(text), "sources": sources[:2]}


def weekly_review(text: str, sources: list[dict], *, authoritative: bool | None = None) -> dict:
    start = re.search(r"review_period_start (\d{4}-\d{2}-\d{2})", text).group(1)
    end = re.search(r"review_period_end (\d{4}-\d{2}-\d{2})", text).group(1)
    org = re.search(r"organization_id (\S+?),", text).group(1)
    dq = _dq_from(text)
    people_unavailable = "People agent" not in text
    doc = {"review_period_start": start, "review_period_end": end, "generated_at": f"{end}T18:00:00+00:00",
           "generated_by_agent": "chief_of_staff", "organization_id": org,
           "authoritative": dq["overall_status"] == "GOOD" if authoritative is None else authoritative,
           "overall_health": {"status": "AMBER", "score": None, "summary": "mixed", "components": []},
           **{k: _section() for k in ("revenue", "profit", "cash", "pipeline", "delivery", "customers", "costs",
                                     "risk")},
           "people": _section("UNAVAILABLE"),
           "top_wins": [], "top_failures": [], "root_causes": [], "decisions_required": [],
           "priorities_next_week": [], "recommendations": [], "watchlist": [], "unresolved_items": [],
           "data_quality": dq, "sources": sources[:2]}
    if dq["overall_status"] != "GOOD":
        doc["authoritative"] = False if authoritative is None else authoritative
    if people_unavailable:
        doc["people"] = _section("UNAVAILABLE")
        if authoritative is None:
            doc["authoritative"] = False      # an unavailable domain is never authoritative
    return doc


def contract_responder(overrides: dict[str, Callable[[ModelSession, str], Any]] | None = None):
    """Respond per agent and output kind with contract-valid JSON citing provided sources."""
    overrides = overrides or {}

    def respond(session: ModelSession, last: str):
        agent = agent_of(session)
        if agent in overrides:
            return overrides[agent](session, last)
        first_user = next(m["text"] for m in session.messages if m.get("role") == "user")
        sources = available_sources(first_user)
        kind = kind_of(first_user)
        if kind == "DAILY_BRIEF":
            return mock_turn(json.dumps(daily_brief(first_user, sources)))
        if kind == "WEEKLY_REVIEW":
            return mock_turn(json.dumps(weekly_review(first_user, sources)))
        return mock_turn(json.dumps(structured(agent, sources)))
    return respond


def runtime_cc(clock, *, responder=None, script=None, models=None, **build_kw):
    from command_center.system import CommandCenter
    mock = MockModelProvider(script=list(script or []), responder=responder)
    providers = ProviderRegistry({"mock": mock, "backup": MockModelProvider(name="backup", responder=responder)})
    cc = CommandCenter.build(clock=clock, models=models or mock_models(), providers=providers, sleep=lambda s: None,
                             **build_kw)
    return cc, mock
