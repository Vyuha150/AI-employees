"""Phase 4 against a real PostgreSQL server: durable queue, database-side queries, conversations,
connector contracts and diagnostics, readiness. Set CC_TEST_DATABASE_URL (admin DSN)."""

import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from command_center.common import ActorType, Principal  # noqa: E402
from command_center.errors import NotFoundError  # noqa: E402
from command_center.executors import ToolInvocation  # noqa: E402
from command_center.jobs import JobQueueRules, JobStatus, SideEffect, new_job  # noqa: E402
from command_center.pg.database import Database, apply_migrations, ensure_organization  # noqa: E402
from command_center.pg.diagnostics import connector_status, migrations_state  # noqa: E402
from command_center.pg.jobs import PgJobQueue  # noqa: E402
from command_center.pg.read_connectors import pg_read_connectors  # noqa: E402
from command_center.pg.repositories import PgRepositories  # noqa: E402
from command_center.policy import OrganizationPolicy  # noqa: E402
from command_center.readiness import Readiness, readiness  # noqa: E402
from command_center.system import CommandCenter  # noqa: E402

from .conftest import AGENT_DIR, FakeClock  # noqa: E402

pytestmark = pytest.mark.postgres
ADMIN = os.environ.get("CC_TEST_DATABASE_URL")
PERMS = frozenset({f"run_agent:{p.stem}" for p in AGENT_DIR.glob("*.yaml")} | {"auto_execute:GREEN", "tasks:write"})
TASK = {"objective": "o", "description": "d", "owner": "cfo", "priority": "P2", "expected_output": "e",
        "completion_criteria": ["c"], "risk_level": "GREEN"}


@pytest.fixture(scope="module")
def dsn():
    if not ADMIN:
        pytest.skip("CC_TEST_DATABASE_URL not set")
    try:
        admin = psycopg.connect(ADMIN, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    name = f"cc_p4_{uuid.uuid4().hex[:10]}"
    admin.execute(f"CREATE DATABASE {name}")
    base = psycopg.conninfo.conninfo_to_dict(ADMIN)
    base["dbname"] = name
    yield psycopg.conninfo.make_conninfo(**base)
    admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
    admin.close()


@pytest.fixture(scope="module")
def db(dsn):
    d = Database(dsn)
    apply_migrations(d)
    yield d
    d.close()


def tenant(db, slug, clock=None):
    pol = replace(OrganizationPolicy.load(), organization_id=slug)
    org = ensure_organization(db, slug, slug, pol.timezone, pol.currency)
    repos = PgRepositories(db, org, slug)
    return CommandCenter.build(policy=pol, repositories=repos, clock=clock or FakeClock(), sleep=lambda s: None), repos, org


def person(org):
    return Principal("founder", org, ActorType.HUMAN, frozenset({"FOUNDER"}), PERMS)


# ---- durable queue ----------------------------------------------------------------------
def test_queue_survives_restart_and_is_idempotent(dsn, db):
    tenant(db, "q-org")
    clock = FakeClock()
    q1 = PgJobQueue(db, clock=clock)
    j, created = q1.enqueue(new_job(organization_id="q-org", job_type="report", idempotency_key="daily:1",
                                    side_effect=SideEffect.READ_ONLY, now=clock(), max_attempts=3))
    assert created and not q1.enqueue(new_job(organization_id="q-org", job_type="report", idempotency_key="daily:1",
                                              side_effect=SideEffect.READ_ONLY, now=clock()))[1]
    restarted = Database(dsn)                          # a new process
    q2 = PgJobQueue(restarted, clock=clock)
    assert q2.get(j.job_id).status is JobStatus.QUEUED
    claimed = q2.claim("worker-a")
    assert claimed.job_id == j.job_id and q2.claim("worker-b") is None      # SKIP LOCKED / single claim
    clock.advance(seconds=claimed.timeout_seconds + 1)
    (reclaimed,) = q2.reclaim_abandoned()
    assert reclaimed.status is JobStatus.QUEUED                            # READ_ONLY: safely reclaimed
    again = q2.claim("worker-b")
    assert q2.ack(again.job_id, "worker-b", "ref").status is JobStatus.SUCCEEDED
    restarted.close()


def test_external_write_jobs_never_retried_in_database(db):
    tenant(db, "q-org2")
    clock = FakeClock()
    q = PgJobQueue(db, JobQueueRules(), clock=clock)
    q.enqueue(new_job(organization_id="q-org2", job_type="execute_approved", idempotency_key="apr:1",
                      side_effect=SideEffect.EXTERNAL_WRITE, now=clock(), timeout_seconds=30))
    c = q.claim("w")
    clock.advance(seconds=31)
    (abandoned,) = q.reclaim_abandoned()
    assert abandoned.status is JobStatus.ABANDONED
    with db.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("UPDATE jobs SET max_attempts = 3 WHERE id = %s", (c.job_id,))


# ---- database-side queries ----------------------------------------------------------------
def test_queries_filter_order_paginate_and_isolate(db):
    cc_a, repos_a, _ = tenant(db, "query-a")
    cc_b, repos_b, _ = tenant(db, "query-b")
    for i in range(5):
        cc_a.tasks.create_from_contract({**TASK, "title": f"A{i}"}, organization_id="query-a", created_by="cfo",
                                        actor_type=ActorType.AGENT)
        cc_a.clock.advance(seconds=1)
    cc_b.tasks.create_from_contract({**TASK, "title": "B0"}, organization_id="query-b", created_by="cfo",
                                    actor_type=ActorType.AGENT)
    page1 = repos_a.tasks.query({"status": "BACKLOG"}, order_by="created_at", limit=2)
    page2 = repos_a.tasks.query({"status": "BACKLOG"}, order_by="created_at", limit=2, offset=2)
    page3 = repos_a.tasks.query({"status": "BACKLOG"}, order_by="created_at", limit=2, offset=4)
    assert [t.title for t in page1 + page2 + page3] == ["A0", "A1", "A2", "A3", "A4"]
    assert [t.title for t in repos_b.tasks.query({})] == ["B0"]
    with pytest.raises(ValueError):
        repos_a.tasks.query({"description": "x"})                       # not an allow-listed column
    p = person("query-a")
    for _ in range(3):
        cc_a.runs.create_run("cfo", p, "x")
    runs = repos_a.runs.query({"agent_id": "cfo", "status__in": ["QUEUED"]}, order_by="-created_at", limit=2)
    assert len(runs) == 2 and repos_b.runs.query({"agent_id": "cfo"}) == []
    evs = repos_a.audit.page(after_sequence=0, limit=3)
    more = repos_a.audit.page(after_sequence=evs[-1].sequence, limit=3)
    assert [e.sequence for e in evs + more] == [1, 2, 3, 4, 5, 6]
    assert all(e.organization_id == "query-a" for e in evs + more)
    cc_a.kpis.record({"kpi": "cash_runway", "value": "6", "unit": "m", "observed_at": "2026-11-01",
                      "source_reference": "erp"}, organization_id="query-a", recorded_by="cfo")
    cc_a.kpis.record({"kpi": "cash_runway", "value": "5", "unit": "m", "observed_at": "2026-11-15",
                      "source_reference": "erp"}, organization_id="query-a", recorded_by="cfo")
    assert [str(s.value) for s in cc_a.kpis.history("query-a", "cash_runway", 1)] == ["5"]
    assert cc_b.kpis.history("query-b", "cash_runway") == []
    with db.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        idx = {r[0] for r in cur.fetchall()}
    assert {"agent_runs_org_status_created", "tasks_org_status_created", "approvals_org_status",
            "outputs_org_kind_created", "recommendations_org_status", "alerts_org_status_created",
            "audit_events_org_seq", "kpi_snapshots_org_key_recorded", "messages_chronological",
            "conversations_org_updated"} <= idx


# ---- conversations --------------------------------------------------------------------------
def test_conversations_persist_immutably_and_isolated(db):
    cc_a, repos_a, _ = tenant(db, "conv-a")
    cc_b, _, _ = tenant(db, "conv-b")
    pa = person("conv-a")
    c = cc_a.conversations.create(pa, title="Pricing", primary_agent_id="sales_director")
    for i in range(5):
        cc_a.conversations.add_message(c.conversation_id, sender_type=ActorType.HUMAN, sender_id="founder",
                                       content=f"msg {i}", attachment_ids=("att-1",) if i == 0 else ())
    page = cc_a.conversations.list_messages(pa, c.conversation_id, limit=2)
    rest = cc_a.conversations.list_messages(pa, c.conversation_id, after_seq=page.next_cursor, limit=10)
    assert [m.content for m in page.items + rest.items] == [f"msg {i}" for i in range(5)]
    assert page.items[0].attachment_ids == ("att-1",)
    with pytest.raises(NotFoundError):
        cc_b.conversations.list_messages(person("conv-b"), c.conversation_id)
    with db.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE messages SET content = 'edited' WHERE conversation_id = %s", (c.conversation_id,))
        cur.execute("SELECT count(*) FROM conversation_participants WHERE conversation_id = %s", (c.conversation_id,))
        assert cur.fetchone()[0] == 2


# ---- connectors ---------------------------------------------------------------------------
def test_connector_contract_states_and_diagnostics(db):
    now = datetime.now(timezone.utc)
    cc, repos, org = tenant(db, "conn-org")
    conns = pg_read_connectors(db, org, lambda a: ())
    inv = lambda tool: ToolInvocation("conn-org", "cfo", "r", tool, tool, {})
    assert conns["read_leads"].fetch(inv("read_leads")).status.value == "UNAVAILABLE"
    with db.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS connector.src_leads (organization_id uuid, source_system text, "
                    "source_record_id text, source_updated_at timestamptz, name text, company text, stage text, score int)")
        cur.execute("INSERT INTO connector.src_leads VALUES (%s, 'hubspot', 'L-1', %s, 'Ann', 'Acme', 'MQL', 70)",
                    (org, now))
        cur.execute("CREATE OR REPLACE VIEW connector.leads AS SELECT * FROM connector.src_leads")
        cur.execute("CREATE TABLE IF NOT EXISTS connector.src_vendors (organization_id uuid, source_system text, "
                    "source_record_id text, source_updated_at timestamptz, name text, status text, lead_time_days int)")
        cur.execute("INSERT INTO connector.src_vendors VALUES (%s, 'erp', 'V-1', %s, 'Steel Co', 'ACTIVE', 9)",
                    (org, now - timedelta(days=30)))
        cur.execute("CREATE OR REPLACE VIEW connector.vendors AS SELECT * FROM connector.src_vendors")
        cur.execute("CREATE OR REPLACE VIEW connector.projects AS SELECT 1 AS id")          # breaks the contract
    ok = conns["read_leads"].fetch(inv("read_leads"))
    rec = ok.data["records"][0]
    assert ok.status.value == "OK" and ok.source_ids == ("db:leads:L-1",)
    for col in ("organization_id", "source_system", "source_record_id", "source_updated_at", "retrieved_at"):
        assert rec[col]
    assert rec["organization_id"] == "conn-org" and rec["source_system"] == "hubspot"
    assert conns["read_vendors"].fetch(inv("read_vendors")).status.value == "STALE"
    bad = conns["read_projects"].fetch(inv("read_projects"))
    assert bad.status.value == "ERROR" and "contract" in bad.error
    status = {r["view"]: r for r in connector_status(db, org)}
    assert status["connector.leads"]["state"] == "OK" and status["connector.leads"]["last_successful_refresh"]
    assert status["connector.vendors"]["state"] == "STALE"
    assert status["connector.projects"]["state"] == "ERROR" and status["connector.customers"]["state"] == "UNAVAILABLE"
    assert "contract" in status["connector.projects"]["latest_error"]


def test_readiness_with_database(dsn, db, tmp_path):
    from .test_config_packaging import org_config
    assert migrations_state(db)["current"]
    d = org_config(tmp_path, strip_placeholders=True)
    env = {"ANTHROPIC_API_KEY": "sk-ant-test-123456789", "CC_DATABASE_URL": dsn}
    ensure_organization(db, "org_example", "Example", "Asia/Kolkata", "INR")
    rep = readiness(d, "production", env)
    checks = {c.name: c for c in rep.checks}
    assert checks["database"].status.value == "OK" and checks["migrations"].status.value == "OK"
    assert checks["queue"].status.value == "OK"
    assert rep.status is Readiness.DEGRADED                     # connector views not all configured
    assert "sk-ant" not in rep.render() and dsn not in rep.render()
    bad = readiness(d, "production", {**env, "CC_DATABASE_URL": "host=/nonexistent port=1 dbname=x"})
    assert bad.status is Readiness.NOT_READY and {c.name: c for c in bad.checks}["database"].status.value == "FAIL"
