"""Process bootstrap and long-running entrypoints: API server, queue worker, scheduler.

    ai-command-center serve       uvicorn + AsgiApp (graceful shutdown on SIGTERM)
    ai-command-center worker      claims jobs; SIGTERM finishes the current job, then exits
    ai-command-center scheduler   one leader (PostgreSQL advisory lock); enqueues agent workflows,
                                  runs escalation evaluation inline
    ai-command-center migrate     apply core migrations (privileged; run before deploying)

Fail closed: production requires a database with current migrations, Supabase auth
configuration and a valid configuration. Development without CC_DATABASE_URL runs in memory.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping

from .api import ApiService
from .commands import CommandService
from .common import new_id, utc_now
from .config import AppConfig, Environment, load_config
from .errors import ConfigurationError
from .jobs import AGENT_RUN, InMemoryJobQueue, JobQueueRules, JobWorker, SideEffect, agent_run_handler, \
    agent_run_reclaim_policy, new_job
from .ratelimit import InMemoryRateLimiter
from .tenancy import TenantRegistry

log = logging.getLogger("command_center.process")
SCHEDULED_WORKFLOW = "scheduled_workflow"
LEADER_LOCK_KEY = "cc_scheduler_leader"


@dataclass
class Deployment:
    config: AppConfig
    tenants: TenantRegistry
    queue: Any
    directory: Any
    limiter: Any
    api: ApiService
    http: Any
    db: Any = None
    providers: Any = None
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def cc(self) -> Any:
        return next(iter(self.tenants.tenants.values()))

    def ready(self) -> tuple[bool, dict[str, str]]:
        checks: dict[str, str] = {"config": "ok"}
        if self.db is not None:
            from .pg.diagnostics import migrations_state, table_exists
            try:
                with self.db.cursor() as cur:
                    cur.execute("SELECT 1")
                checks["database"] = "ok"
                checks["migrations"] = "ok" if migrations_state(self.db)["current"] else "pending"
                checks["queue"] = "ok" if table_exists(self.db, "jobs") else "missing"
            except Exception as exc:
                checks["database"] = f"unreachable ({type(exc).__name__})"
        else:
            checks["database"] = "in-memory" if self.config.environment is not Environment.PRODUCTION else "missing"
            checks["queue"] = "in-memory" if self.config.environment is not Environment.PRODUCTION else "missing"
        models = self.config.models
        default = models.profile(models.default_profile)
        configured = self.providers is not None and default.provider in getattr(self.providers, "providers", {})
        checks["model_provider"] = "ok" if configured else "not configured"
        prod = self.config.environment is Environment.PRODUCTION
        required = ["config", "database", "migrations", "queue"] + (["model_provider"] if prod else [])
        acceptable = ("ok",) if prod else ("ok", "in-memory")
        ok = all(checks[k] in acceptable for k in required if k in checks)
        return ok, checks


def build_deployment(env: Mapping[str, str] | None = None, *, config_dir: Any = None, clock: Callable = utc_now,
                     providers: Any = None, verifier: Any = None, directory: Any = None) -> Deployment:
    from .http_api import HttpApi
    from .model_providers import ProviderRegistry
    from .system import CommandCenter

    env = dict(os.environ if env is None else env)
    cfg = load_config(config_dir, env=env)
    prod = cfg.environment is Environment.PRODUCTION
    dsn = env.get("CC_DATABASE_URL")
    if prod and not dsn:
        raise ConfigurationError("production requires CC_DATABASE_URL")
    if providers is None:
        try:
            providers = ProviderRegistry.from_config(cfg.models, env=env)
        except ConfigurationError:
            if prod:
                raise
            providers = ProviderRegistry()          # development: runs fail closed without a provider
    tenants = TenantRegistry()
    db = None
    if dsn:
        from .pg.database import Database, ensure_organization
        from .pg.diagnostics import migrations_state
        from .pg.jobs import PgJobQueue
        from .pg.membership import PgMembershipDirectory, PgRateLimiter
        from .pg.repositories import PgRepositories
        db = Database(dsn)
        state = migrations_state(db)
        if not state["current"]:
            raise ConfigurationError(f"migrations pending: {state['pending']} (run `ai-command-center migrate`)")
        pol = cfg.policy
        org = ensure_organization(db, pol.organization_id, pol.name, pol.timezone, pol.currency)
        cc = CommandCenter.from_config(cfg, providers=providers, repositories=PgRepositories(db, org, pol.organization_id),
                                       clock=clock)
        tenants.register(cc)
        queue = PgJobQueue(db, JobQueueRules(safe_to_reclaim=agent_run_reclaim_policy(tenants)), clock=clock)
        directory = directory or PgMembershipDirectory(db)
        limiter = PgRateLimiter(db, clock)
    else:
        from .auth import InMemoryMembershipDirectory
        cc = CommandCenter.from_config(cfg, providers=providers, clock=clock)
        tenants.register(cc)
        queue = InMemoryJobQueue(JobQueueRules(safe_to_reclaim=agent_run_reclaim_policy(tenants)), clock=clock)
        directory = directory or InMemoryMembershipDirectory()
        limiter = InMemoryRateLimiter(clock)
    if verifier is None and env.get("CC_SUPABASE_URL"):
        from .auth import AuthConfig, TokenVerifier
        verifier = TokenVerifier(AuthConfig.from_env(env))
    if prod and verifier is None:
        raise ConfigurationError("production requires Supabase auth (CC_SUPABASE_URL and a JWT secret or JWKS)")
    api = ApiService(tenants, CommandService(tenants, queue=queue))
    dep = Deployment(cfg, tenants, queue, directory, limiter, api, None, db, providers, env)
    dep.http = HttpApi(api, verifier, directory, limiter, health=dep.ready, production=prod)
    return dep


# ---------------------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------------------
def worker_handlers(dep: Deployment) -> dict[str, Callable]:
    return {AGENT_RUN: agent_run_handler(dep.tenants), SCHEDULED_WORKFLOW: scheduled_workflow_handler(dep)}


def run_worker(dep: Deployment, stop: threading.Event, *, idle_sleep: float = 1.0, reclaim_every: float = 30.0,
               max_iterations: int | None = None, worker_id: str | None = None) -> int:
    """Claim-and-run loop. Stopping never drops queued jobs: unclaimed jobs stay QUEUED; a job
    interrupted by a crash is handled by lease expiry and the queue's reclaim rules."""
    worker = JobWorker(dep.queue, worker_handlers(dep), worker_id or new_id("worker"))
    processed, last_reclaim, i = 0, 0.0, 0
    log.info("worker started", extra={"worker_id": worker.worker_id, "durable": getattr(dep.queue, "durable", False)})
    while not stop.is_set() and (max_iterations is None or i < max_iterations):
        i += 1
        now = time.monotonic()
        if now - last_reclaim >= reclaim_every:
            for j in dep.queue.reclaim_abandoned():
                log.warning("job lease expired", extra={"job_id": j.job_id, "status": j.status.value})
            last_reclaim = now
            _heartbeat(dep, worker.worker_id, "worker", False)
        job = worker.run_once()
        if job is None:
            stop.wait(idle_sleep)
            continue
        processed += 1
        log.info("job finished", extra={"job_id": job.job_id, "job_type": job.job_type, "status": job.status.value,
                                        "attempts": job.attempts})
    log.info("worker stopped", extra={"worker_id": worker.worker_id, "processed": processed})
    return processed


def scheduled_workflow_handler(dep: Deployment) -> Callable:
    from types import SimpleNamespace

    from .briefing import ScheduledWorkflowDispatcher

    def handle(job: Any) -> str | None:
        cc = dep.tenants.tenants[job.organization_id]
        run_as = scheduler_principal(dep, job.organization_id)
        if run_as is None:
            raise RuntimeError("CC_SCHEDULER_USER_ID is not set or is not a member; agent workflows cannot run")
        when = __import__("datetime").datetime.fromisoformat(job.payload["scheduled_for"])
        fake = SimpleNamespace(workflow=job.payload["workflow"], timezone=job.payload["timezone"],
                               job_id=job.payload["job_id"])
        return ScheduledWorkflowDispatcher(cc, cc.runtime, run_as)(fake, when)
    return handle


def scheduler_principal(dep: Deployment, org: str) -> Any:
    """The scheduler acts as an explicit service account whose roles/permissions are loaded from
    the membership directory like any other user. Nothing is implied."""
    from .auth import Identity, resolve_principal
    uid = dep.env.get("CC_SCHEDULER_USER_ID")
    if not uid:
        return None
    try:
        return resolve_principal(Identity(uid, None, None, None, 0), dep.directory, org, {org})
    except Exception:
        return None


# ---------------------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------------------
class InProcessLeaderLock:
    """Leadership within one process group (development/tests)."""

    _held: dict[str, str] = {}
    _lock = threading.Lock()

    def __init__(self, owner: str):
        self.owner = owner

    def acquire(self) -> bool:
        with self._lock:
            holder = self._held.get(LEADER_LOCK_KEY)
            if holder in (None, self.owner):
                self._held[LEADER_LOCK_KEY] = self.owner
                return True
            return False

    def release(self) -> None:
        with self._lock:
            if self._held.get(LEADER_LOCK_KEY) == self.owner:
                del self._held[LEADER_LOCK_KEY]


class PgLeaderLock:
    """Session-level advisory lock on a dedicated connection: released automatically if the
    process dies, so a standby takes over without duplicate leadership."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = None

    def acquire(self) -> bool:
        import psycopg
        try:
            if self.conn is None or self.conn.closed:
                self.conn = psycopg.connect(self.dsn, autocommit=True)
            row = self.conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LEADER_LOCK_KEY,)).fetchone()
            return bool(row[0])
        except Exception:
            self.conn = None
            return False

    def release(self) -> None:
        if self.conn is not None and not self.conn.closed:
            try:
                self.conn.execute("SELECT pg_advisory_unlock_all()")
            finally:
                self.conn.close()
        self.conn = None


def queue_dispatcher(dep: Deployment, org: str) -> Callable:
    """Scheduler dispatch: escalation evaluation inline (fast, no model); agent workflows are
    enqueued (durable, idempotent per occurrence) for the worker."""
    from .briefing import ScheduledWorkflowDispatcher
    from .idempotency import idempotency_key

    def dispatch(job: Any, when: Any) -> str | None:
        cc = dep.tenants.tenants[org]
        if job.workflow == "escalation_evaluation":
            return ScheduledWorkflowDispatcher(cc, cc.runtime, None)(job, when)
        queued, created = dep.queue.enqueue(new_job(
            organization_id=org, job_type=SCHEDULED_WORKFLOW,
            idempotency_key=idempotency_key(org, "scheduled_workflow", job.job_id, when.isoformat()),
            side_effect=SideEffect.INTERNAL, now=cc.clock(), timeout_seconds=1800,
            payload={"job_id": job.job_id, "workflow": job.workflow, "timezone": job.timezone,
                     "scheduled_for": when.isoformat()}))
        return f"job:{queued.job_id}"
    return dispatch


def run_scheduler(dep: Deployment, stop: threading.Event, *, lock: Any = None, tick_seconds: float = 30.0,
                  max_ticks: int | None = None, process_id: str | None = None) -> int:
    process_id = process_id or new_id("scheduler")
    lock = lock or (PgLeaderLock(dep.env["CC_DATABASE_URL"]) if dep.db is not None else InProcessLeaderLock(process_id))
    org = dep.cc.policy.organization_id
    sched = dep.cc.scheduler(queue_dispatcher(dep, org))
    ticks, was_leader = 0, False
    try:
        while not stop.is_set() and (max_ticks is None or ticks < max_ticks):
            leader = lock.acquire()
            if leader and not was_leader:
                sched.sync_jobs()
                log.info("scheduler leadership acquired", extra={"process_id": process_id})
            elif was_leader and not leader:
                log.warning("scheduler leadership lost", extra={"process_id": process_id})
            was_leader = leader
            _heartbeat(dep, process_id, "scheduler", leader)
            if leader:
                for r in sched.tick():
                    log.info("schedule", extra={"job": r.job_id, "status": r.status.value, "reason": r.reason})
                ticks += 1
            elif max_ticks is not None:
                ticks += 1
            stop.wait(tick_seconds)
    finally:
        lock.release()
    return ticks


def _heartbeat(dep: Deployment, process_id: str, kind: str, leader: bool) -> None:
    if dep.db is None:
        return
    try:
        with dep.db.cursor() as cur:
            cur.execute("INSERT INTO process_heartbeats (process_id, kind, is_leader, started_at, last_seen_at) "
                        "VALUES (%s, %s, %s, now(), now()) ON CONFLICT (process_id) DO UPDATE SET "
                        "is_leader = EXCLUDED.is_leader, last_seen_at = now()", (process_id, kind, leader))
    except Exception:
        log.warning("heartbeat failed", extra={"process_id": process_id})


def install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, frame: Any) -> None:
        log.info("shutdown requested", extra={"signal": signum})
        stop.set()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def serve(dep: Deployment, host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError:
        raise ConfigurationError("uvicorn is required: pip install 'ai-command-center[api]'") from None
    from .asgi import AsgiApp
    origins = tuple(o.strip() for o in dep.env.get("CC_CORS_ORIGINS", "").split(",") if o.strip())
    app = AsgiApp(dep.http, cors_origins=origins)
    uvicorn.run(app, host=host, port=port, log_config=None, timeout_graceful_shutdown=20, proxy_headers=True)


def health_server(dep: Deployment, port: int) -> threading.Thread:
    """Tiny liveness/readiness server for worker and scheduler processes."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health/live":
                code, body = 200, {"status": "live"}
            elif self.path == "/health/ready":
                ok, checks = dep.ready()
                code, body = (200 if ok else 503), {"status": "ready" if ok else "not_ready", "checks": checks}
            else:
                code, body = 404, {"status": "not found"}
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return t


__all__ = ["Deployment", "build_deployment", "run_worker", "run_scheduler", "serve", "timedelta"]
