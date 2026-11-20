"""Parts 24-27 against a real PostgreSQL server.

Set CC_TEST_DATABASE_URL to an admin DSN (e.g. "host=/tmp/pg port=5432 user=postgres dbname=postgres").
Each module run creates and drops its own database. Skipped when no server is configured.
"""

import os
import uuid
from datetime import timedelta

import pytest

psycopg = pytest.importorskip("psycopg")

from command_center.audit import AuditEvent, EventType  # noqa: E402
from command_center.common import ActorType, Principal  # noqa: E402
from command_center.errors import ConcurrencyError, PermissionDeniedError  # noqa: E402
from command_center.gateway import ActionStatus  # noqa: E402
from command_center.pg.database import Database, apply_migrations, ensure_organization  # noqa: E402
from command_center.pg.read_connectors import pg_read_connectors  # noqa: E402
from command_center.pg.repositories import PgRepositories  # noqa: E402
from command_center.policy import OrganizationPolicy  # noqa: E402
from command_center.resources import resource  # noqa: E402
from command_center.state_machines import ApprovalStatus, RunStatus  # noqa: E402
from command_center.system import CommandCenter  # noqa: E402

from .conftest import AGENT_DIR, FakeClock, action  # noqa: E402

pytestmark = pytest.mark.postgres
ADMIN = os.environ.get("CC_TEST_DATABASE_URL")
PERMS = frozenset({f"run_agent:{p.stem}" for p in AGENT_DIR.glob("*.yaml")} | {"auto_execute:GREEN"})


@pytest.fixture(scope="module")
def dsn():
    if not ADMIN:
        pytest.skip("CC_TEST_DATABASE_URL not set")
    try:
        admin = psycopg.connect(ADMIN, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    name = f"cc_test_{uuid.uuid4().hex[:10]}"
    admin.execute(f"CREATE DATABASE {name}")
    base = psycopg.conninfo.conninfo_to_dict(ADMIN)
    base["dbname"] = name
    yield psycopg.conninfo.make_conninfo(**base)
    admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
    admin.close()


@pytest.fixture(scope="module")
def db(dsn):
    d = Database(dsn)
    assert apply_migrations(d) == ["0001_core.sql", "0002_operations.sql", "0003_api.sql"]
    assert apply_migrations(d) == []                    # idempotent
    yield d
    d.close()


def tenant(db, slug, clock=None):
    pol = OrganizationPolicy.load()
    from dataclasses import replace
    pol = replace(pol, organization_id=slug)
    org = ensure_organization(db, slug, slug, pol.timezone, pol.currency)
    repos = PgRepositories(db, org, slug)
    cc = CommandCenter.build(policy=pol, repositories=repos, clock=clock or FakeClock(), sleep=lambda s: None)
    return cc, repos, org


def person(org, pid="founder", roles=("FOUNDER", "FINANCE_APPROVER")):
    return Principal(pid, org, ActorType.HUMAN, frozenset(roles), PERMS)


def test_full_governed_flow_persists(db):
    cc, repos, _ = tenant(db, "acme")
    p = person("acme")
    run = cc.runs.start(cc.runs.create_run("procurement", p, "restock").run_id)
    o = cc.gateway.submit(run.run_id, action("create_purchase_order", {"amount": 2000, "currency": "INR"}, risk="GREEN"))
    assert o.status is ActionStatus.PENDING_APPROVAL
    cc.approve(o.approval_id, person("acme", "fin", ("FINANCE_APPROVER",)))
    with pytest.raises(Exception):
        cc.execute_approved(o.approval_id, tool_name="create_purchase_order", action_type="create_purchase_order",
                            payload={"amount": 20000, "currency": "INR"})
    r = cc.execute_approved(o.approval_id, tool_name="create_purchase_order", action_type="create_purchase_order",
                            payload={"amount": 2000, "currency": "INR"})
    assert r.status is ActionStatus.EXECUTED
    # reload through a fresh adapter: everything round-trips through the codec
    fresh = PgRepositories(db, repos.org_uuid, "acme")
    assert fresh.approvals.get(o.approval_id).status is ApprovalStatus.EXECUTED
    assert fresh.runs.get(run.run_id).status is RunStatus.RUNNING
    fresh.audit.verify_chain()
    calls = fresh.tool_calls.list(run.run_id)
    assert [c["tool_name"] for c in calls] == ["create_purchase_order"] and calls[0]["mock"] is True
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM approval_events WHERE organization_id = %s", (repos.org_uuid,))
        assert cur.fetchone()[0] >= 4
        cur.execute("SELECT count(*) FROM agents WHERE organization_id = %s", (repos.org_uuid,))
        assert cur.fetchone()[0] == 12


def test_audit_is_append_only_in_the_database(db):
    cc, repos, org = tenant(db, "append-only")
    cc.runs.create_run("cfo", person("append-only"), "x")
    with db.cursor() as cur:
        for sql in ("UPDATE audit_events SET actor_id = 'x' WHERE organization_id = %s",
                    "DELETE FROM audit_events WHERE organization_id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql, (org,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("TRUNCATE audit_events CASCADE")
    with pytest.raises(PermissionDeniedError):
        repos.audit.append(AuditEvent("e", cc.clock(), "other-org", ActorType.SYSTEM, "s", None, None,
                                      EventType.CONFIGURATION_CHANGED, "x", "y"))


def test_approval_binding_immutable_in_database(db):
    cc, repos, org = tenant(db, "binding")
    p = person("binding")
    run = cc.runs.start(cc.runs.create_run("procurement", p, "x").run_id)
    o = cc.gateway.submit(run.run_id, action("create_purchase_order", {"amount": 5, "currency": "INR"}, risk="AMBER"))
    with db.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE approvals SET payload_hash = repeat('0', 64) WHERE id = %s", (o.approval_id,))
        with pytest.raises(psycopg.errors.CheckViolation):     # self-approval blocked by constraint
            cur.execute("UPDATE approvals SET approved_by = requested_by WHERE id = %s", (o.approval_id,))


def test_cross_organization_references_rejected(db):
    cc_a, repos_a, org_a = tenant(db, "tenant-a")
    cc_b, repos_b, org_b = tenant(db, "tenant-b")
    run_a = cc_a.runs.create_run("cfo", person("tenant-a"), "x")
    with db.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):   # org B row pointing at org A's run
            cur.execute("INSERT INTO kpi_snapshots (id, organization_id, kpi_key, value, unit, observed_at, "
                        "source_reference, recorded_by, run_id, record, created_at) VALUES "
                        "(gen_random_uuid(), %s, 'k', 1, 'u', 'now', 'src', 'cfo', %s, '{}', now())",
                        (org_b, run_a.run_id))
    assert repos_b.runs.find(run_a.run_id) is None
    assert repos_b.runs.list() == []
    with pytest.raises(PermissionDeniedError):
        repos_b.runs.add(run_a)


def test_optimistic_concurrency(db):
    cc, repos, _ = tenant(db, "concurrency")
    run = cc.runs.create_run("cfo", person("concurrency"), "x")
    stale = repos.runs.get(run.run_id)
    cc.runs.start(run.run_id)
    from dataclasses import replace
    with pytest.raises(ConcurrencyError):
        repos.runs.update(replace(stale, objective="changed"), expected_version=stale.version)


def test_idempotency_and_scheduler_in_postgres(db):
    from datetime import datetime, timezone
    clock = FakeClock(datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc))
    cc, repos, _ = tenant(db, "sched", clock)
    assert repos.idempotency.claim("sched", "k1")[0] and not repos.idempotency.claim("sched", "k1")[0]
    seen = []
    s = cc.scheduler(lambda job, when: seen.append(job.job_id) or "ref")
    s.sync_jobs()
    clock.advance(hours=1, minutes=5)
    s.tick()
    s2 = cc.scheduler(lambda job, when: seen.append(job.job_id) or "ref")     # a second scheduler instance
    from dataclasses import replace
    j = repos.scheduled_jobs.get("daily_brief")
    repos.scheduled_jobs.update(replace(j, watermark=clock() - timedelta(hours=2)), expected_version=j.version)
    s2.tick()
    assert seen.count("daily_brief") == 1


def test_read_connectors_use_views_and_never_invent(db):
    cc, repos, org = tenant(db, "reads")
    conns = pg_read_connectors(db, org, lambda a: cc.registry.get_profile(a).knowledge_scopes)
    from command_center.executors import ToolInvocation
    inv = ToolInvocation("reads", "cfo", "r", "read_customers", "read_customers", {})
    assert conns["read_customers"].fetch(inv).status.value == "UNAVAILABLE"
    with db.cursor() as cur:
        cur.execute("CREATE TABLE connector.customer_src (organization_id uuid, record_id text, name text, "
                    "updated_at timestamptz)")
        cur.execute("INSERT INTO connector.customer_src VALUES (%s, 'c-1', 'Acme', now())", (org,))
        cur.execute("CREATE VIEW connector.customers AS SELECT * FROM connector.customer_src")
    r = conns["read_customers"].fetch(inv)
    assert r.status.value == "OK" and r.source_ids == ("db:customers:c-1",) and r.data["records"][0]["name"] == "Acme"


RLS_STUB = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN; END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
GRANT USAGE ON SCHEMA auth TO authenticated, anon;
GRANT EXECUTE ON FUNCTION auth.uid() TO authenticated, anon;
"""


def test_supabase_rls_isolates_tenants(db):
    cc_a, repos_a, org_a = tenant(db, "rls-a")
    cc_b, repos_b, org_b = tenant(db, "rls-b")
    cc_a.tasks.create_from_contract({"title": "A task", "objective": "o", "description": "d", "owner": "cfo",
                                     "priority": "P2", "expected_output": "e", "completion_criteria": ["c"],
                                     "risk_level": "GREEN"}, organization_id="rls-a", created_by="cfo",
                                    actor_type=ActorType.AGENT)
    cc_b.tasks.create_from_contract({"title": "B task", "objective": "o", "description": "d", "owner": "cfo",
                                     "priority": "P2", "expected_output": "e", "completion_criteria": ["c"],
                                     "risk_level": "GREEN"}, organization_id="rls-b", created_by="cfo",
                                    actor_type=ActorType.AGENT)
    user_a = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(RLS_STUB)
        cur.execute(resource("migrations", "supabase", "0101_rls.sql").read_text())
        cur.execute("INSERT INTO organization_members (organization_id, user_id) VALUES (%s, %s)", (org_a, user_a))
    with db.transaction() as cur:
        cur.execute("SET LOCAL ROLE authenticated")
        cur.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (user_a,))
        cur.execute("SELECT title FROM tasks")
        assert [r[0] for r in cur.fetchall()] == ["A task"]           # never org B
        cur.execute("SELECT count(*) FROM audit_events")
        assert cur.fetchone()[0] == 0                                  # no audit:read permission
        cur.execute("SAVEPOINT s1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE approvals SET status = 'APPROVED'")
        cur.execute("ROLLBACK TO SAVEPOINT s1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("INSERT INTO permissions (organization_id, role_id, permission) VALUES (%s, %s, 'x')",
                        (org_a, str(uuid.uuid4())))
