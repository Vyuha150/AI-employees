"""Phase 4 Parts 2 (queue), 3 (scheduled escalation), 4 (conversations), 8 (fallback), 9 (briefing), 11 (readiness)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from command_center.audit import EventType
from command_center.briefing import ScheduledWorkflowDispatcher
from command_center.commands import CommandRequest, CommandService
from command_center.common import ActorType, Principal
from command_center.errors import ConfigurationError, InvalidTransitionError, NotFoundError, PolicyViolationError
from command_center.executors import ToolResult
from command_center.jobs import (
    AGENT_RUN,
    InMemoryJobQueue,
    JobQueueRules,
    JobStatus,
    JobWorker,
    SideEffect,
    TransientJobError,
    agent_run_handler,
    agent_run_reclaim_policy,
    new_job,
)
from command_center.policy import OrganizationPolicy
from command_center.readiness import Readiness, readiness
from command_center.scheduler import ScheduledRunStatus
from command_center.state_machines import RunStatus
from command_center.tenancy import TenantRegistry

from .conftest import ORG, FakeClock
from .helpers import contract_responder, mock_models, runtime_cc


# ---- queue --------------------------------------------------------------------------------
def job(clock, key="k1", side=SideEffect.READ_ONLY, **kw):
    return new_job(organization_id=ORG, job_type=kw.pop("job_type", "read_report"), idempotency_key=key,
                   side_effect=side, now=clock(), **kw)


def test_queue_idempotent_priority_ack(clock):
    q = InMemoryJobQueue(clock=clock)
    a, created = q.enqueue(job(clock, "a", priority=1))
    dup, created2 = q.enqueue(job(clock, "a", priority=9))
    assert created and not created2 and dup.job_id == a.job_id
    q.enqueue(job(clock, "b", priority=5))
    first = q.claim("w1")
    assert first.idempotency_key == "b" and first.status is JobStatus.CLAIMED and first.attempts == 1
    with pytest.raises(InvalidTransitionError):
        q.ack(first.job_id, "w2")                              # only the claimant may ack
    assert q.ack(first.job_id, "w1", "ref").status is JobStatus.SUCCEEDED
    with pytest.raises(PolicyViolationError):
        q.enqueue(job(clock, "x", side=SideEffect.EXTERNAL_WRITE, max_attempts=3))


def test_retry_rules(clock):
    q = InMemoryJobQueue(clock=clock)
    q.enqueue(job(clock, "read", max_attempts=3))
    q.enqueue(job(clock, "write", side=SideEffect.EXTERNAL_WRITE, job_type="execute_approved"))
    r = q.claim("w")
    r = q.fail(r.job_id, "w", "timeout", retryable=True)
    assert r.status is JobStatus.QUEUED and r.available_at > clock()        # backoff
    w = q.claim("w")
    assert w.idempotency_key == "write"
    w = q.fail(w.job_id, "w", "provider timeout", retryable=True)
    assert w.status is JobStatus.FAILED                                     # never auto-retried
    with pytest.raises(PolicyViolationError):
        q.retry(w.job_id)
    clock.advance(minutes=5)
    r = q.claim("w")
    r = q.fail(r.job_id, "w", "boom", retryable=False)
    assert r.status is JobStatus.FAILED
    assert q.retry(r.job_id).status is JobStatus.QUEUED                     # explicit retry of a safe job


def test_abandoned_jobs_reclaimed_only_when_safe(clock):
    abandoned = []
    q = InMemoryJobQueue(clock=clock, on_abandoned=abandoned.append)
    q.enqueue(job(clock, "read", max_attempts=2, timeout_seconds=60))
    q.enqueue(job(clock, "write", side=SideEffect.EXTERNAL_WRITE, job_type="execute_approved", timeout_seconds=60))
    q.claim("dead-worker"), q.claim("dead-worker")
    clock.advance(seconds=61)
    states = {j.idempotency_key: j.status for j in q.reclaim_abandoned()}
    assert states == {"read": JobStatus.QUEUED, "write": JobStatus.ABANDONED}
    assert [j.idempotency_key for j in abandoned] == ["write"]


def test_worker_transient_and_permanent_errors(clock):
    q = InMemoryJobQueue(clock=clock)
    q.enqueue(job(clock, "t", max_attempts=2))
    calls = []

    def handler(j):
        calls.append(j.attempts)
        raise TransientJobError("db busy") if len(calls) == 1 else RuntimeError("bad input")
    w = JobWorker(q, {"read_report": handler}, "w")
    assert w.run_once().status is JobStatus.QUEUED
    clock.advance(minutes=1)
    assert w.run_once().status is JobStatus.FAILED and calls == [1, 2]


def queued_command_setup(clock):
    cc, _ = runtime_cc(clock, responder=contract_responder())
    tenants = TenantRegistry()
    tenants.register(cc)
    q = InMemoryJobQueue(clock=clock, rules=JobQueueRules(safe_to_reclaim=agent_run_reclaim_policy(tenants)))
    return cc, tenants, q, CommandService(tenants, queue=q)


def test_commands_go_through_the_queue_once(clock, founder):
    cc, tenants, q, svc = queued_command_setup(clock)
    r1 = svc.submit(founder, CommandRequest(ORG, "cfo", "Cash position?", idempotency_key="client-123"))
    assert r1.status == "QUEUED" and cc.runs.get(r1.run_id).status is RunStatus.QUEUED
    r2 = svc.submit(founder, CommandRequest(ORG, "cfo", "Cash position?", idempotency_key="client-123"))
    assert r2.status == "DUPLICATE" and r2.run_id == r1.run_id
    assert len(cc.runs.repo.list()) == 1 and len(q.list(ORG)) == 1
    JobWorker(q, {AGENT_RUN: agent_run_handler(tenants)}, "w").drain()
    assert cc.runs.get(r1.run_id).status is RunStatus.SUCCEEDED
    assert q.list(ORG)[0].status is JobStatus.SUCCEEDED


def test_abandoned_agent_run_reclaimed_only_if_never_started(clock, founder):
    cc, tenants, q, svc = queued_command_setup(clock)
    r = svc.submit(founder, CommandRequest(ORG, "cfo", "x"))
    claimed = q.claim("crashed")
    clock.advance(seconds=claimed.timeout_seconds + 1)
    assert q.reclaim_abandoned()[0].status is JobStatus.QUEUED            # run still QUEUED: safe
    claimed = q.claim("crashed-again")
    cc.runs.start(r.run_id)                                                # started, then the worker died
    clock.advance(seconds=claimed.timeout_seconds + 1)
    assert q.reclaim_abandoned()[0].status is JobStatus.ABANDONED


# ---- scheduled escalation ---------------------------------------------------------------------
TASK = {"title": "Collect AR", "objective": "cash", "description": "d", "owner": "cfo", "priority": "P0",
        "expected_output": "e", "completion_criteria": ["c"], "risk_level": "GREEN", "due_date": "2026-11-18"}


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self, inv):
        return ToolResult(True, "r", {"records": self.rows}, source_ids=tuple(f"db:{inv.tool_name}:{r['source_record_id']}"
                                                                              for r in self.rows))


def test_scheduler_runs_escalation_evaluation_from_policy(founder):
    clock = FakeClock(datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc))
    old = (clock() - timedelta(days=20)).isoformat()
    connectors = {
        "read_opportunities": Rows([{"source_record_id": "o1", "name": "Acme", "amount": 900000,
                                     "last_activity_at": old}]),
        "read_customers": Rows([{"source_record_id": "c1", "name": "Beta", "health_score": 22}]),
        "read_projects": Rows([{"source_record_id": "p1", "name": "Rollout", "status": "BLOCKED",
                                "blocked_since": (clock() - timedelta(days=3)).isoformat()}]),
    }
    cc, _ = runtime_cc(clock, responder=contract_responder(), read_connectors=connectors)
    cc.tasks.create_from_contract(TASK, organization_id=ORG, created_by="cfo", actor_type=ActorType.AGENT)
    cc.tasks.create_from_contract({**TASK, "title": "File GST return", "priority": "P2", "owner": "legal_risk",
                                   "due_date": "2026-11-25", "kpi_affected": ["missed_compliance_events"]},
                                  organization_id=ORG, created_by="legal_risk", actor_type=ActorType.AGENT)
    sched = cc.scheduler(ScheduledWorkflowDispatcher(cc, cc.runtime, founder))
    sched.sync_jobs()
    assert sched.jobs.get("escalation_evaluation").enabled
    clock.advance(minutes=16)
    results = [r for r in sched.tick() if r.job_id == "escalation_evaluation"]
    assert results and results[-1].status is ScheduledRunStatus.TRIGGERED
    rules = {a.category for a in cc.alerts.list(ORG)}
    assert {"ESCALATION:TASK_OVERDUE", "ESCALATION:DEAL_STALLED", "ESCALATION:CUSTOMER_HEALTH_LOW",
            "ESCALATION:PROJECT_BLOCKED", "ESCALATION:COMPLIANCE_DEADLINE"} <= rules
    count = len(cc.alerts.list(ORG))
    clock.advance(days=1)                       # next day, conditions unresolved
    sched.tick()
    assert len(cc.alerts.list(ORG)) == count    # no duplicate alerts for open conditions
    assert cc.escalations.suppressed > 0


def test_resolved_condition_can_alert_again(cc, founder, clock):
    from .conftest import action, running_run
    run = running_run(cc, "cfo", founder)
    for _ in range(3):
        cc.gateway.submit(run.run_id, action("create_alert", {"severity": "WARNING"}))
    (first,) = [e for e in cc.escalations.evaluate().triggered if e.rule_id == "tool_failures"]
    assert not [e for e in cc.escalations.evaluate().triggered if e.rule_id == "tool_failures"]
    cc.alerts.resolve(first.alert.alert_id, founder, "connector fixed")
    clock.advance(days=1)
    for _ in range(3):
        cc.gateway.submit(run.run_id, action("create_alert", {"severity": "WARNING"}))
    assert [e for e in cc.escalations.evaluate().triggered if e.rule_id == "tool_failures"]


def test_escalation_reports_missing_connectors(cc):
    from command_center.escalations import collect_signals
    signals, notes = collect_signals(cc.read_tools, ORG, cc.clock())
    assert signals == [] and len(notes) == 3
    rep = cc.escalations.evaluate(signals=signals)
    assert any("customer_health_critical" in n for n in rep.not_evaluable)


# ---- conversations ------------------------------------------------------------------------
def test_conversation_persistence_pagination_isolation_redaction(cc, founder):
    convs = cc.conversations
    c = convs.create(founder, title="Q4 planning", primary_agent_id="cfo")
    for i in range(7):
        convs.add_message(c.conversation_id, sender_type=ActorType.HUMAN, sender_id="founder", content=f"m{i}")
    p1 = convs.list_messages(founder, c.conversation_id, limit=3)
    p2 = convs.list_messages(founder, c.conversation_id, after_seq=p1.next_cursor, limit=3)
    p3 = convs.list_messages(founder, c.conversation_id, after_seq=p2.next_cursor, limit=3)
    assert [m.content for m in p1.items + p2.items + p3.items] == [f"m{i}" for i in range(7)]
    assert p3.next_cursor is None
    other = Principal("x", "org_other", ActorType.HUMAN)
    with pytest.raises(Exception):
        convs.list_messages(other, c.conversation_id)
    leaked = convs.add_message(c.conversation_id, sender_type=ActorType.HUMAN, sender_id="founder",
                               content="use key sk-ant-api03-abcdefghijk and password=hunter2",
                               structured_content={"api_key": "zzz", "note": "ok"}, attachment_ids=("f1",))
    assert "sk-ant" not in leaked.content and "hunter2" not in leaked.content
    assert leaked.structured_content == {"api_key": "[REDACTED CREDENTIAL]", "note": "ok"}
    assert leaked.attachment_ids == ("f1",)
    assert cc.audit.store.events(event_type=EventType.MESSAGE_REDACTED)
    assert not hasattr(convs, "update_message") and not hasattr(convs, "delete_message")
    with pytest.raises(Exception):
        leaked.content = "edited"                          # frozen record


def test_commands_persist_conversation_but_do_not_feed_history(clock, founder):
    cc, mock = runtime_cc(clock, responder=contract_responder())
    tenants = TenantRegistry()
    tenants.register(cc)
    svc = CommandService(tenants)
    r1 = svc.submit(founder, CommandRequest(ORG, "cfo", "SECRET_PLAN_ALPHA details"))
    r2 = svc.submit(founder, CommandRequest(ORG, "cfo", "Second question", conversation_id=r1.conversation_id))
    msgs = cc.conversations.list_messages(founder, r1.conversation_id).items
    assert [m.sender_type for m in msgs] == [ActorType.HUMAN, ActorType.AGENT, ActorType.HUMAN, ActorType.AGENT]
    assert "SECRET_PLAN_ALPHA" not in mock.calls[-1]["input"]          # history not auto-fed
    with pytest.raises(NotFoundError):
        svc.submit(founder, CommandRequest(ORG, "cfo", "x", conversation_id="00000000-0000-0000-0000-000000000000"))
    assert r2.conversation_id == r1.conversation_id


# ---- fallback metadata (Part 8) -------------------------------------------------------------
def test_fallback_records_requested_model_reason_and_time(clock, founder):
    from command_center.agents import RunContext
    from command_center.model_providers import ModelProviderError
    cc, mock = runtime_cc(clock, responder=contract_responder(), models=mock_models(fallback=True))
    mock.fail_with = [ModelProviderError("capacity exceeded", retryable=False, provider="mock", model="mock-model")]
    run = cc.runs.create_run("cfo", founder, "x")
    cc.runtime.execute(run.run_id, RunContext("o", (), (), "x", founder))
    (ev,) = cc.audit.store.events(event_type=EventType.MODEL_FALLBACK_USED)
    p = ev.payload
    assert p["requested_model"] == "mock-model" and p["fallback_model"] == "backup-model"
    assert "capacity exceeded" in p["reason"] and p["run_id"] == run.run_id and p["timestamp"]
    r = cc.runs.get(run.run_id)
    assert r.fallback_used and "capacity exceeded" in r.fallback_reason and r.fallback_at == clock()


def test_fallback_off_by_default_in_packaged_config():
    from command_center.config import load_config
    assert load_config(environment="test", env={}).models.fallback.allowed is False


# ---- briefing configuration (Part 9) ---------------------------------------------------------
def test_brief_participants_come_from_policy(clock, founder):
    from command_center.briefing import BriefingService
    from command_center.policy import BriefingPolicy
    pol = OrganizationPolicy.load()
    pol = replace(pol, briefing=replace(pol.briefing, daily_specialists=("cfo", "operations")))
    cc, _ = runtime_cc(clock, responder=contract_responder(), policy=pol)
    res = BriefingService(cc, cc.runtime).generate_daily_brief(founder, datetime(2026, 11, 20).date())
    assert res.status == "GENERATED" and set(res.invoked_agents) == {"data_analyst", "cfo", "operations",
                                                                     "chief_of_staff"}
    assert isinstance(pol.briefing, BriefingPolicy)


def test_people_agent_cannot_be_a_core_brief_participant():
    raw = dict(OrganizationPolicy.load().raw)
    raw["briefing"] = {"daily": {"data_agent": "data_analyst", "specialists": ["people_performance"]},
                       "weekly": {"core": [], "conditional": {}}}
    with pytest.raises(ConfigurationError, match="PEOPLE_AUTHORIZED"):
        OrganizationPolicy.from_mapping(raw)
    raw["briefing"] = {"daily": {"data_agent": "data_analyst", "specialists": []},
                       "weekly": {"core": [], "conditional": {"ghost": "ALWAYS"}}}
    with pytest.raises(ConfigurationError, match="unknown condition"):
        OrganizationPolicy.from_mapping(raw)


def test_people_data_remains_disabled():
    pol = OrganizationPolicy.load()
    assert not pol.privacy_gate_open("read_people_summary")
    assert not pol.privacy_gates["read_people_summary"].approved


# ---- readiness (Part 11) -----------------------------------------------------------------
def test_readiness_states():
    dev = readiness(environment="development", env={})
    assert dev.status is Readiness.DEGRADED
    prod = readiness(environment="production", env={"ANTHROPIC_API_KEY": "sk-ant-x-123456789"})
    assert prod.status is Readiness.NOT_READY
    names = {c.name: c for c in prod.checks}
    assert names["placeholders"].status.value == "FAIL" and names["configuration"].status.value == "FAIL"
    assert names["people data"].detail.startswith("read_people_summary disabled")
    assert "sk-ant" not in prod.render()
    for required in ("configuration", "placeholders", "database", "migrations", "queue", "scheduler",
                     "model provider", "connectors", "disabled tools", "external-write adapters", "people data",
                     "security issues"):
        assert required in names
    unset = readiness(env={})
    assert unset.status is Readiness.NOT_READY


def test_people_data_stays_unavailable_to_people_agent(cc, founder):
    from .conftest import action, running_run
    run = running_run(cc, "people_performance", founder)
    assert cc.gateway.submit(run.run_id, action("read_people_summary", {})).status.value == "DENIED"


def test_conversation_api_isolation_and_pagination(clock):
    from command_center.api import ApiService
    from .test_api_tenancy import member, org_policy
    tenants = TenantRegistry()
    ccs = {}
    for org in ("org_a", "org_b"):
        cc, _ = runtime_cc(clock, responder=contract_responder(), policy=org_policy(org))
        tenants.register(cc)
        ccs[org] = cc
    api = ApiService(tenants)
    a = member("org_a")
    receipt = api.create_command(a, agent_id="cfo", message="first")
    conv = receipt["conversation_id"]
    api.create_command(a, agent_id="cfo", message="second", conversation_id=conv)
    page = api.list_messages(a, conv, limit=3)
    assert [m["seq"] for m in page["items"]] == [1, 2, 3] and page["next_cursor"] == 3
    assert [m["seq"] for m in api.list_messages(a, conv, after_seq=3)["items"]] == [4]
    with pytest.raises(NotFoundError):
        api.list_messages(member("org_b"), conv)
    assert api.list_conversations(member("org_b")) == [] and len(api.list_conversations(a)) == 1
