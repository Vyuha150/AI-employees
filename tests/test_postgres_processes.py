"""Phase 5 against PostgreSQL: server-side memberships, shared rate limits, scheduler
leadership and queued work surviving a worker/process restart."""

import json
import os
import threading
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from command_center.bootstrap import PgLeaderLock, build_deployment, run_worker  # noqa: E402
from command_center.http_api import HttpRequest  # noqa: E402
from command_center.jobs import JobStatus  # noqa: E402
from command_center.model_providers import MockModelProvider, ProviderRegistry  # noqa: E402
from command_center.pg.database import Database, apply_migrations  # noqa: E402
from command_center.pg.membership import PgRateLimiter, add_member  # noqa: E402
from command_center.ratelimit import Limit, RateLimitedError  # noqa: E402
from command_center.state_machines import RunStatus  # noqa: E402

from .api_helpers import FULL, SECRET, token  # noqa: E402
from .conftest import FakeClock  # noqa: E402
from .helpers import contract_responder  # noqa: E402
from .test_processes import mock_config  # noqa: E402

pytestmark = pytest.mark.postgres
ADMIN = os.environ.get("CC_TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def dsn():
    if not ADMIN:
        pytest.skip("CC_TEST_DATABASE_URL not set")
    try:
        admin = psycopg.connect(ADMIN, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    name = f"cc_proc_{uuid.uuid4().hex[:10]}"
    admin.execute(f"CREATE DATABASE {name}")
    base = psycopg.conninfo.conninfo_to_dict(ADMIN)
    base["dbname"] = name
    url = psycopg.conninfo.make_conninfo(**base)
    d = Database(url)
    apply_migrations(d)
    d.close()
    yield url
    admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
    admin.close()


def deploy(dsn, tmp_path, clock):
    env = {"CC_ENVIRONMENT": "development", "CC_DATABASE_URL": dsn,
           "CC_SUPABASE_URL": "https://example-project.supabase.co", "CC_SUPABASE_JWT_SECRET": SECRET}
    return build_deployment(env, config_dir=mock_config(tmp_path), clock=clock,
                            providers=ProviderRegistry({"mock": MockModelProvider(responder=contract_responder())}))


def org_uuid_of(dep):
    with dep.db.cursor() as cur:
        cur.execute("SELECT id FROM organizations WHERE slug = %s", ("org_example",))
        return str(cur.fetchone()[0])


def call(dep, method, path, user, body=None, **claims):
    h = {"authorization": f"Bearer {token(user, **claims)}"}
    r = dep.http.handle(HttpRequest(method, path, {}, h, b"" if body is None else json.dumps(body).encode()))
    return r.status, r.body


def test_identity_and_roles_come_from_the_database(dsn, tmp_path):
    dep = deploy(dsn, tmp_path, FakeClock())
    try:
        user, stranger = str(uuid.uuid4()), str(uuid.uuid4())
        org_uuid = org_uuid_of(dep)
        add_member(dep.db, org_uuid, user, {"ANALYST": ["run_agent:cfo"]}, display_name="Analyst")
        status, body = call(dep, "GET", "/v1/me", user, app_metadata={"roles": ["FOUNDER"]},
                            organization_id="org_other", permissions=["*"])
        assert status == 200
        me = body["data"]
        assert me["organization_id"] == "org_example" and me["roles"] == ["ANALYST"]
        assert me["permissions"] == ["run_agent:cfo"]
        assert call(dep, "GET", "/v1/me", stranger)[0] == 403                 # valid JWT, no membership
        assert call(dep, "POST", "/v1/commands", user, {"requested_agent_id": "marketing_director", "message": "x"})[0] == 403
        assert call(dep, "POST", "/v1/commands", user, {"requested_agent_id": "cfo", "message": "x"})[0] == 201
    finally:
        dep.db.close()


def test_queued_command_survives_api_and_worker_restart(dsn, tmp_path):
    clock = FakeClock()
    api_proc = deploy(dsn, tmp_path / "a", clock)
    user = str(uuid.uuid4())
    add_member(api_proc.db, org_uuid_of(api_proc), user, {"FOUNDER": FULL})
    status, body = call(api_proc, "POST", "/v1/commands", user, {"requested_agent_id": "cfo", "message": "cash?"})
    assert status == 201
    run_id = body["data"]["run_id"]
    crashed = api_proc.queue.claim("worker-crashed")                             # claimed, never acked
    assert crashed is not None
    api_proc.db.close()                                                          # process gone

    clock.advance(seconds=crashed.timeout_seconds + 1)
    worker = deploy(dsn, tmp_path / "b", clock)                                  # fresh process, same database
    try:
        run_worker(worker, threading.Event(), idle_sleep=0, reclaim_every=0, max_iterations=3)
        assert worker.cc.runs.get(run_id).status is RunStatus.SUCCEEDED
        assert worker.queue.get(crashed.job_id).status is JobStatus.SUCCEEDED     # reclaimed, run exactly once
        assert worker.cc.runs.get(crashed.run_id).status is RunStatus.SUCCEEDED
        assert all(j.status is JobStatus.SUCCEEDED and j.attempts <= 2 for j in worker.queue.list("org_example"))
    finally:
        worker.db.close()


def test_rate_limits_are_shared_between_processes(dsn):
    clock = FakeClock()
    a, b = Database(dsn), Database(dsn)
    try:
        la, lb = PgRateLimiter(a, clock), PgRateLimiter(b, clock)
        lim = Limit("commands:user", 3, 60)
        key = f"commands:user:{uuid.uuid4()}"
        la.hit(key, lim)
        lb.hit(key, lim)
        la.hit(key, lim)
        with pytest.raises(RateLimitedError):
            lb.hit(key, lim)
        clock.advance(seconds=61)
        lb.hit(key, lim)                                                         # new window
    finally:
        a.close()
        b.close()


def test_scheduler_leadership_is_exclusive(dsn):
    first, second = PgLeaderLock(dsn), PgLeaderLock(dsn)
    try:
        assert first.acquire() and first.acquire()                               # leader keeps leading
        assert not second.acquire()
        first.release()                                                          # leader exits / dies
        assert second.acquire()
        assert not first.acquire()
    finally:
        first.release()
        second.release()
