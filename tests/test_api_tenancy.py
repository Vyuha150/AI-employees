"""Parts 26, 28, 36, 44-48: API layer, multi-tenancy, commands, attachments, realtime, privacy gate."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from command_center.api import ApiService
from command_center.attachments import Attachment, Classification, ScanStatus
from command_center.audit import EventType
from command_center.commands import AttachmentStore, CommandService, ThreadExecutor
from command_center.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from command_center.policy import OrganizationPolicy, PrivacyGate
from command_center.read_tools import PeopleSummaryConnector
from command_center.realtime import REALTIME_EVENT_TYPES, RealtimeBus
from command_center.tenancy import TenantRegistry
from command_center.tools import PROPOSED_TOOLS

from .conftest import AGENT_DIR, action, human, running_run
from .helpers import contract_responder, runtime_cc

ALL_AGENTS = {f"run_agent:{p.stem}" for p in AGENT_DIR.glob("*.yaml")}


def org_policy(org):
    return replace(OrganizationPolicy.load(), organization_id=org)


def member(org, pid="founder", perms=(), roles=("FOUNDER", "FINANCE_APPROVER")):
    return human(pid, roles=set(roles), permissions=ALL_AGENTS | {"auto_execute:GREEN"} | set(perms), org=org)


@pytest.fixture
def two_tenants(clock):
    tenants = TenantRegistry()
    ccs = {}
    for org in ("org_a", "org_b"):
        cc, _ = runtime_cc(clock, responder=contract_responder(), policy=org_policy(org))
        tenants.register(cc)
        ccs[org] = cc
    return ApiService(tenants), ccs


def test_users_never_see_other_organizations(two_tenants):
    api, ccs = two_tenants
    a, b = member("org_a"), member("org_b")
    receipt = api.create_command(a, agent_id="cfo", message="How is cash?")
    run_id = receipt["run_id"]
    assert api.get_run(a, run_id)["status"] == "SUCCEEDED"
    with pytest.raises(NotFoundError):
        api.get_run(b, run_id)
    assert api.list_runs(b) == [] and len(api.list_runs(a)) == 1
    assert api.list_outputs(b) == [] and api.list_outputs(a)
    out_id = api.list_outputs(a)[0]["output_id"]
    with pytest.raises(NotFoundError):
        api.get_output(b, out_id)
    # an approval in org A cannot be decided from org B
    run = running_run(ccs["org_a"], "procurement", a)
    o = ccs["org_a"].gateway.submit(run.run_id, action("create_purchase_order", {"amount": 5, "currency": "INR"},
                                                       risk="AMBER"))
    with pytest.raises(NotFoundError):
        api.approve_action(member("org_b", "fin_b"), o.approval_id)
    assert api.approve_action(member("org_a", "fin_a"), o.approval_id)["status"] == "APPROVED"
    unknown = human("x", org="org_zzz")
    with pytest.raises(PermissionDeniedError):
        api.list_agents(unknown)


def test_agents_are_organization_scoped(two_tenants):
    api, ccs = two_tenants
    assert {x["agent_id"] for x in api.list_agents(member("org_a"))} == {x["agent_id"] for x in api.list_agents(member("org_b"))}
    assert ccs["org_a"].registry is not ccs["org_b"].registry       # (organization_id, agent_key) scoping


def test_write_permissions_enforced(two_tenants):
    api, _ = two_tenants
    viewer = member("org_a", "viewer", roles=())
    task = {"title": "t", "objective": "o", "description": "d", "owner": "cfo", "priority": "P2",
            "expected_output": "e", "completion_criteria": ["c"], "risk_level": "GREEN"}
    with pytest.raises(PermissionDeniedError, match="tasks:write"):
        api.create_task(viewer, task)
    writer = member("org_a", "writer", perms={"tasks:write"})
    t = api.create_task(writer, task)
    assert api.update_task(writer, t["task_id"], "IN_PROGRESS")["status"] == "IN_PROGRESS"
    with pytest.raises(PermissionDeniedError):
        api.list_audit_events(viewer)
    assert api.list_audit_events(member("org_a", "auditor", perms={"audit:read"}))
    with pytest.raises(PermissionDeniedError):
        api.get_agent_performance(viewer, "cfo")
    perf = api.get_agent_performance(member("org_a", "p", perms={"performance:read"}), "cfo", "7d")
    assert perf["period"] == "7d"


def test_recommendation_and_evaluation_endpoints(clock):
    from tests.test_end_to_end import response
    cc, _ = runtime_cc(clock)
    tenants = TenantRegistry()
    tenants.register(cc)
    api = ApiService(tenants)
    p = member("org_example", perms={"recommendations:decide", "outputs:review", "changes:write"})
    run = running_run(cc, "procurement", p)
    rec = {"problem_or_opportunity": "Lead time rose.", "evidence": ["vendor history"], "evidence_source_ids": ["s1"],
           "recommendation": "Qualify a second vendor.", "why_now": "stock low", "expected_impact": "reliability",
           "impact_category": "OPERATIONAL_RELIABILITY", "estimated_value": None, "estimated_cost": None,
           "effort": "MEDIUM", "urgency": "HIGH", "dependencies": [], "risks": [], "reversible": True,
           "confidence": "MEDIUM", "suggested_owner": "procurement", "kpis_affected": ["supplier_reliability"]}
    out = cc.outputs.record(cc.runs.get(run.run_id), response("procurement", recs=[rec]))
    cc.ingest_output(out.output_id)
    (r,) = api.list_recommendations(p)
    assert api.approve_recommendation(p, r["recommendation_id"])["status"] == "APPROVED"
    ev = api.submit_output_evaluation(p, out.output_id, accuracy=5, relevance=4, completeness=4, actionability=5,
                                      business_value=4, decision="APPROVED")
    assert ev["composite_score"] == "4.40"
    with pytest.raises(PermissionDeniedError):
        api.approve_recommendation(member("org_example", "v", roles=()), r["recommendation_id"])


def test_command_org_mismatch_and_async_executor(clock):
    cc, _ = runtime_cc(clock, responder=contract_responder())
    tenants = TenantRegistry()
    tenants.register(cc)
    svc = CommandService(tenants, executor=ThreadExecutor(workers=1))
    p = member("org_example")
    from command_center.commands import CommandRequest
    with pytest.raises(PermissionDeniedError):
        svc.submit(p, CommandRequest("org_other", "cfo", "hi"))
    with pytest.raises(Exception):
        svc.submit(p, CommandRequest("org_example", "ghost_agent", "hi"))
    receipt = svc.submit(p, CommandRequest("org_example", "cfo", "Summarize AR"))
    assert receipt.run_id and receipt.status in ("QUEUED", "RUNNING", "SUCCEEDED")
    svc.executor._pool.shutdown(wait=True)
    assert cc.runs.get(receipt.run_id).status.value == "SUCCEEDED"
    assert cc.audit.store.events(event_type=EventType.COMMAND_RECEIVED)
    assert svc.messages[0].text == "Summarize AR"


def attachment(**kw):
    base = dict(file_id="f1", organization_id="org_example", name="q3.pdf", mime_type="application/pdf", size=1000,
                storage_reference="bucket/q3.pdf", uploaded_by="founder", created_at=datetime.now(timezone.utc),
                classification=Classification.INTERNAL, scan_status=ScanStatus.CLEAN)
    base.update(kw)
    return Attachment(**base)


class Extractor:
    def extract(self, att):
        return f"text of {att.name}"


@pytest.mark.parametrize("kw, reason", [
    ({"scan_status": ScanStatus.PENDING}, "scan status"),
    ({"mime_type": "application/x-msdownload"}, "MIME"),
    ({"classification": Classification.RESTRICTED}, "classification"),
    ({"size": 10**9}, "size"),
])
def test_attachments_are_gated(clock, kw, reason):
    cc, _ = runtime_cc(clock, responder=contract_responder())
    tenants = TenantRegistry()
    tenants.register(cc)
    store = AttachmentStore()
    store.add(attachment(**kw))
    svc = CommandService(tenants, attachments=store, extractor=Extractor())
    from command_center.commands import CommandRequest
    with pytest.raises(ValidationFailedError, match=reason):
        svc.submit(member("org_example"), CommandRequest("org_example", "cfo", "read this", ("f1",)))
    assert cc.audit.store.events(event_type=EventType.ATTACHMENT_REJECTED)


def test_clean_attachment_reaches_model_with_source(clock):
    cc, mock = runtime_cc(clock, responder=contract_responder())
    tenants = TenantRegistry()
    tenants.register(cc)
    store = AttachmentStore()
    store.add(attachment())
    svc = CommandService(tenants, attachments=store, extractor=Extractor())
    from command_center.commands import CommandRequest
    receipt = svc.submit(member("org_example"), CommandRequest("org_example", "cfo", "read this", ("f1",)))
    assert cc.runs.get(receipt.run_id).status.value == "SUCCEEDED"
    assert "[usr:att-f1]" in mock.calls[0]["input"] and "text of q3.pdf" in mock.calls[0]["input"]
    other = attachment(file_id="f2", organization_id="org_other")
    store.add(other)
    with pytest.raises(NotFoundError):
        svc.submit(member("org_example"), CommandRequest("org_example", "cfo", "x", ("f2",)))


def test_realtime_events_are_sanitized(clock, founder):
    cc, _ = runtime_cc(clock, responder=contract_responder())
    bus = RealtimeBus()
    got = []
    bus.subscribe("org_example", got.append)
    bus.subscribe("org_other", lambda e: pytest.fail("cross-tenant event"))
    cc.audit.listeners.append(bus.on_audit_event)
    run = running_run(cc, "procurement", founder)
    cc.gateway.submit(run.run_id, action("create_purchase_order", {"amount": 5, "currency": "INR",
                                                                   "note": "password=hunter2"},
                                         risk="AMBER", reason="sk-ant-api03-SECRET"))
    types = [e.event_type for e in got]
    assert {"run.queued", "run.started", "run.tool_requested", "approval.requested", "run.waiting_approval"} <= set(types)
    assert set(types) <= REALTIME_EVENT_TYPES
    blob = repr([e.payload_summary for e in got])
    assert "hunter2" not in blob and "sk-ant" not in blob and "amount" not in blob


# ---- memory retrieval (Part 48) --------------------------------------------------------
def test_only_accepted_memories_and_stale_flagged(cc, clock, founder):
    from command_center.sources import SourceCatalog
    agent = cc.registry.get_agent("cfo")
    src = SourceCatalog.from_response({"sources": [{"id": "s1", "type": "DOCUMENT", "reference": "doc:policy"}]})
    base = {"content": "Net-30 default", "rationale": "policy", "source_ids": ["s1"], "scope": "ORGANIZATION"}
    accepted = cc.memory.propose({**base, "review_at": (clock() + timedelta(days=1)).isoformat()}, agent=agent,
                                 organization_id="org_example", run_id=None, sources=src)
    cc.memory.accept(accepted.memory_id, founder)
    proposed = cc.memory.propose(base, agent=agent, organization_id="org_example", run_id=None, sources=src)
    rejected = cc.memory.propose(base, agent=agent, organization_id="org_example", run_id=None, sources=src)
    cc.memory.reject(rejected.memory_id, founder, "wrong")
    ids = {m.memory_id for m in cc.memory.context_for(agent, "org_example")}
    assert ids == {accepted.memory_id} and proposed.memory_id not in ids
    clock.advance(days=2)
    due = cc.memory.review_due(cc.memory.context_for(agent, "org_example"))
    assert [m.memory_id for m in due] == [accepted.memory_id]
    assert cc.memory.repo.get(accepted.memory_id).status.value == "ACCEPTED"      # flagged, not deleted
    assert cc.audit.store.events(event_type=EventType.MEMORY_REVIEW_DUE)


# ---- people data privacy gate (Part 36) ------------------------------------------------
def test_people_data_unavailable_until_privacy_review(cc, founder):
    run = running_run(cc, "people_performance", founder)
    out = cc.gateway.submit(run.run_id, action("read_people_summary", {}))
    assert out.status.value == "DENIED" and "privacy-gated" in out.reason
    assert "read_people_summary" in PROPOSED_TOOLS and not cc.tools.exists("read_people_summary")


def test_privacy_gate_requires_reviewer_and_filters_fields():
    pol = OrganizationPolicy.load()
    raw = dict(pol.raw)
    raw["privacy_gates"] = {"read_people_summary": {"approved": True, "reviewed_by": None, "reviewed_at": None,
                                                    "allowed_fields": ["role"]}}
    p = OrganizationPolicy.from_mapping(raw)
    assert not p.privacy_gate_open("read_people_summary")
    conn = PeopleSummaryConnector(lambda org: [{"employee_id": "e1", "role": "AE", "religion": "x", "health": "y",
                                               "salary": 1, "workload": 0.9}],
                                  allowed_fields={"employee_id", "role", "workload", "religion", "health"})
    from command_center.executors import ToolInvocation
    r = conn.fetch(ToolInvocation("o", "people_performance", "r", "read_people_summary", "read", {}))
    assert r.data["records"] == [{"employee_id": "e1", "role": "AE", "workload": 0.9}]
    assert "sensitive personal data excluded" in r.limitations[0]


def test_privacy_gate_approved_enables_tool_only_with_review(clock, founder):
    pol = OrganizationPolicy.load()
    gate = PrivacyGate("read_people_summary", True, "dpo@org", "2026-11-01", ("employee_id", "role"))
    pol = replace(pol, privacy_gates={"read_people_summary": gate})
    from command_center.agents import AgentRegistry
    from command_center.system import CommandCenter
    from .conftest import load_raw_profiles
    raws = load_raw_profiles()
    raws["people_performance"]["allowed_tools"].append("read_people_summary")
    with pytest.raises(Exception):   # default registry has no such tool: profile invalid
        AgentRegistry.from_mappings(raws).get_agent("people_performance")
    tools = __import__("command_center.tools", fromlist=["DEFAULT_TOOL_REGISTRY"]).DEFAULT_TOOL_REGISTRY.with_proposed(
        "read_people_summary")
    reg = AgentRegistry.from_mappings(raws, tools)
    rows = lambda org: [{"employee_id": "e1", "role": "AE", "family": "private"}]
    cc = CommandCenter.build(clock=clock, policy=pol, registry=reg, tools=tools,
                             read_connectors={"read_people_summary": PeopleSummaryConnector(rows, set(gate.allowed_fields))})
    run = running_run(cc, "people_performance", founder)
    out = cc.gateway.submit(run.run_id, action("read_people_summary", {}))
    assert out.status.value == "EXECUTED" and out.result.data["records"] == [{"employee_id": "e1", "role": "AE"}]
