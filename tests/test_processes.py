"""Phase 5: deployment bootstrap, worker, scheduler entrypoints and health."""

import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from command_center.auth import Membership
from command_center.bootstrap import (
    SCHEDULED_WORKFLOW,
    InProcessLeaderLock,
    build_deployment,
    run_scheduler,
    run_worker,
)
from command_center.commands import CommandRequest
from command_center.errors import ConfigurationError
from command_center.jobs import JobStatus
from command_center.state_machines import RunStatus

from .api_helpers import FULL
from .conftest import FakeClock
from .helpers import contract_responder
from command_center.model_providers import MockModelProvider, ProviderRegistry

ROOT = Path(__file__).resolve().parents[1]
PKG_CONFIG = ROOT / "src" / "command_center" / "resources" / "config"


def mock_config(tmp_path: Path) -> Path:
    d = tmp_path / "cfg"
    shutil.copytree(PKG_CONFIG, d)
    m = yaml.safe_load((d / "models.yaml").read_text())
    for name in ("standard", "deep"):
        m["model_profiles"][name] = {"provider": "mock", "model": "mock-model", "max_tokens": 1000, "effort": "low"}
    (d / "models.yaml").write_text(yaml.safe_dump(m))
    return d


def deployment(tmp_path, clock, **env):
    providers = ProviderRegistry({"mock": MockModelProvider(responder=contract_responder())})
    return build_deployment({"CC_ENVIRONMENT": "development", **env}, config_dir=mock_config(tmp_path),
                            clock=clock, providers=providers)


def principal_for(dep, user):
    from command_center.auth import Identity, resolve_principal
    return resolve_principal(Identity(user, None, None, None, 0), dep.directory, "org_example", {"org_example"})


def test_production_requires_database_and_auth(tmp_path):
    with pytest.raises(ConfigurationError):
        build_deployment({"CC_ENVIRONMENT": "production"}, config_dir=mock_config(tmp_path))


def test_worker_processes_queue_and_stops_gracefully(tmp_path):
    clock = FakeClock()
    dep = deployment(tmp_path, clock)
    user = str(uuid.uuid4())
    dep.directory.add(Membership("org_example", user, frozenset({"FOUNDER"}), frozenset(FULL)))
    p = principal_for(dep, user)
    receipts = [dep.api.commands.submit(p, CommandRequest("org_example", "cfo", f"q{i}")) for i in range(3)]
    stop = threading.Event()
    assert run_worker(dep, stop, idle_sleep=0, max_iterations=2) == 2        # stopped after two jobs
    queued = [j for j in dep.queue.list("org_example") if j.status is JobStatus.QUEUED]
    assert len(queued) == 1                                                 # nothing lost
    stop.set()
    assert run_worker(dep, stop) == 0                                       # stop requested: claims nothing
    run_worker(dep, threading.Event(), idle_sleep=0, max_iterations=2)
    assert all(dep.cc.runs.get(r.run_id).status is RunStatus.SUCCEEDED for r in receipts)


def test_worker_restart_reclaims_unstarted_runs(tmp_path):
    clock = FakeClock()
    dep = deployment(tmp_path, clock)
    user = str(uuid.uuid4())
    dep.directory.add(Membership("org_example", user, frozenset({"FOUNDER"}), frozenset(FULL)))
    r = dep.api.commands.submit(principal_for(dep, user), CommandRequest("org_example", "cfo", "x"))
    crashed = dep.queue.claim("worker-that-crashed")                         # claimed, never acked
    clock.advance(seconds=crashed.timeout_seconds + 1)
    run_worker(dep, threading.Event(), idle_sleep=0, reclaim_every=0, max_iterations=2)
    assert dep.cc.runs.get(r.run_id).status is RunStatus.SUCCEEDED
    assert dep.queue.get(crashed.job_id).status is JobStatus.SUCCEEDED


def test_scheduler_leadership_escalation_and_queued_workflows(tmp_path):
    clock = FakeClock(datetime(2026, 11, 23, 1, 55, tzinfo=timezone.utc))      # Mon 07:25 IST
    svc = str(uuid.uuid4())
    dep = deployment(tmp_path, clock, CC_SCHEDULER_USER_ID=svc)
    dep.directory.add(Membership("org_example", svc, frozenset({"FOUNDER"}), frozenset(FULL)))
    leader, standby = InProcessLeaderLock("s1"), InProcessLeaderLock("s2")
    stop = threading.Event()
    assert run_scheduler(dep, stop, lock=leader, tick_seconds=0, max_ticks=1, process_id="s1") == 1
    leader._held["cc_scheduler_leader"] = "s1"                                # s1 still leads
    assert not standby.acquire()
    clock.advance(minutes=8)                                                  # 07:33 IST: brief + escalation due
    run_scheduler(dep, stop, lock=leader, tick_seconds=0, max_ticks=1, process_id="s1")
    jobs = [j for j in dep.queue.list("org_example") if j.job_type == SCHEDULED_WORKFLOW]
    assert any(j.payload["workflow"] == "daily_brief" for j in jobs)
    runs_before = len(dep.cc.runs.repo.list())
    assert runs_before == 0                                                   # scheduler itself runs no agents
    run_worker(dep, threading.Event(), idle_sleep=0, max_iterations=len(jobs))
    assert any(o.kind.value == "DAILY_BRIEF" for o in dep.cc.outputs.repo.list())
    leader.release()
    assert standby.acquire()                                                  # failover
    standby.release()


def test_scheduled_agent_workflow_needs_service_account(tmp_path):
    clock = FakeClock(datetime(2026, 11, 23, 1, 55, tzinfo=timezone.utc))
    dep = deployment(tmp_path, clock)                                         # no CC_SCHEDULER_USER_ID
    lock = InProcessLeaderLock("solo")
    run_scheduler(dep, threading.Event(), lock=lock, tick_seconds=0, max_ticks=1)
    clock.advance(minutes=8)
    run_scheduler(dep, threading.Event(), lock=lock, tick_seconds=0, max_ticks=1)
    lock.release()
    run_worker(dep, threading.Event(), idle_sleep=0, max_iterations=20)
    failed = [j for j in dep.queue.list("org_example") if j.job_type == SCHEDULED_WORKFLOW]
    assert failed and all(j.status is JobStatus.FAILED and "CC_SCHEDULER_USER_ID" in j.last_error for j in failed)
    assert dep.cc.runs.repo.list() == []                                      # nothing ran without identity


def test_deployment_health(tmp_path):
    dep = deployment(tmp_path, FakeClock())
    ready, checks = dep.ready()
    assert ready and checks["database"] == "in-memory" and checks["model_provider"] == "ok"
    assert dep.http.handle(__import__("command_center.http_api", fromlist=["HttpRequest"]).HttpRequest(
        "GET", "/health/ready")).status == 200


def test_cli_exposes_process_commands():
    out = subprocess.run([sys.executable, "-m", "command_center.cli", "--help"], capture_output=True, text=True,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    for cmd in ("serve", "worker", "scheduler", "migrate", "openapi", "readiness"):
        assert cmd in out.stdout
