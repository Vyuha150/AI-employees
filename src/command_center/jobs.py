"""Durable job queue contract (Part 2 of the Phase 4 brief).

`JobQueue` is the provider-neutral interface; `InMemoryJobQueue` serves development and tests,
`pg.jobs.PgJobQueue` is the durable production implementation (jobs survive restarts; claims use
SELECT ... FOR UPDATE SKIP LOCKED). Providers are replaceable behind the same interface.

Safety rules (enforced by every implementation through `JobQueueRules`):
- one job per (organization_id, idempotency_key): duplicate submissions return the existing job;
- side_effect EXTERNAL_WRITE jobs are never retried or reclaimed automatically. A failure or an
  abandoned lease ends in FAILED / ABANDONED for a human (the gateway separately marks the
  approval OUTCOME_UNKNOWN when it cannot prove the provider's result);
- READ_ONLY jobs may retry up to max_attempts with backoff;
- INTERNAL jobs retry only when the caller says the failure is retryable AND the job type is
  declared idempotent; abandoned INTERNAL jobs are reclaimed only when `safe_to_reclaim` says so
  (e.g. an agent run that never started).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from .common import Clock, StrEnum, new_id, utc_now
from .errors import ConcurrencyError, InvalidTransitionError, NotFoundError, PolicyViolationError


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"      # lease expired and not safe to reclaim: needs a human


class SideEffect(StrEnum):
    READ_ONLY = "READ_ONLY"
    INTERNAL = "INTERNAL"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.ABANDONED})


@dataclass(frozen=True)
class Job:
    job_id: str
    organization_id: str
    job_type: str
    idempotency_key: str
    side_effect: SideEffect
    payload: Mapping[str, Any]
    created_at: datetime
    available_at: datetime
    priority: int = 0                     # higher runs first
    run_id: str | None = None
    workflow_run_id: str | None = None
    attempts: int = 0
    max_attempts: int = 1
    timeout_seconds: int = 900
    status: JobStatus = JobStatus.QUEUED
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    result_ref: str | None = None
    version: int = 0


def new_job(*, organization_id: str, job_type: str, idempotency_key: str, side_effect: SideEffect,
            payload: Mapping[str, Any] | None = None, now: datetime, priority: int = 0, run_id: str | None = None,
            workflow_run_id: str | None = None, max_attempts: int = 1, timeout_seconds: int = 900,
            delay: timedelta = timedelta(0)) -> Job:
    if not idempotency_key:
        raise PolicyViolationError("every job needs an idempotency key")
    if side_effect is SideEffect.EXTERNAL_WRITE and max_attempts != 1:
        raise PolicyViolationError("EXTERNAL_WRITE jobs run at most once (max_attempts must be 1)")
    if max_attempts < 1 or timeout_seconds <= 0:
        raise PolicyViolationError("max_attempts >= 1 and timeout_seconds > 0 are required")
    return Job(new_id("job"), organization_id, job_type, idempotency_key, SideEffect(side_effect),
               dict(payload or {}), now, now + delay, priority, run_id, workflow_run_id, 0, max_attempts,
               timeout_seconds)


@dataclass
class JobQueueRules:
    """Retry / reclaim decisions shared by all queue providers."""

    idempotent_job_types: frozenset[str] = frozenset()
    retry_backoff_seconds: float = 30.0
    safe_to_reclaim: Callable[[Job], bool] = lambda job: False

    def may_retry(self, job: Job, retryable: bool) -> bool:
        if job.side_effect is SideEffect.EXTERNAL_WRITE or job.attempts >= job.max_attempts:
            return False
        if job.side_effect is SideEffect.READ_ONLY:
            return retryable
        return retryable and job.job_type in self.idempotent_job_types

    def may_reclaim(self, job: Job) -> bool:
        if job.side_effect is SideEffect.EXTERNAL_WRITE or job.attempts >= job.max_attempts:
            return False
        if job.side_effect is SideEffect.READ_ONLY:
            return True
        return bool(self.safe_to_reclaim(job))

    def backoff(self, job: Job) -> timedelta:
        return timedelta(seconds=self.retry_backoff_seconds * (2 ** max(job.attempts - 1, 0)))


class JobQueue(Protocol):
    def enqueue(self, job: Job) -> tuple[Job, bool]: ...                       # (job, created)
    def claim(self, worker_id: str, job_types: frozenset[str] | None = None) -> Job | None: ...
    def ack(self, job_id: str, worker_id: str, result_ref: str | None = None) -> Job: ...
    def fail(self, job_id: str, worker_id: str, error: str, *, retryable: bool = False) -> Job: ...
    def retry(self, job_id: str) -> Job: ...                                   # explicit retry of a safe failed job
    def cancel(self, job_id: str, reason: str) -> Job: ...
    def get(self, job_id: str) -> Job: ...
    def find_by_key(self, organization_id: str, idempotency_key: str) -> Job | None: ...
    def reclaim_abandoned(self) -> list[Job]: ...
    def list(self, organization_id: str, status: JobStatus | None = None, limit: int = 100) -> list[Job]: ...


@dataclass
class InMemoryJobQueue:
    """Development/test queue. Not durable: state is lost when the process exits."""

    rules: JobQueueRules = field(default_factory=JobQueueRules)
    clock: Clock = utc_now
    on_abandoned: Callable[[Job], None] | None = None
    _jobs: dict[str, Job] = field(default_factory=dict)
    _keys: dict[tuple[str, str], str] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    durable = False

    def _save(self, old: Job, **changes: Any) -> Job:
        cur = self._jobs[old.job_id]
        if cur.version != old.version:
            raise ConcurrencyError(f"job {old.job_id} changed concurrently")
        new = replace(old, version=old.version + 1, **changes)
        self._jobs[old.job_id] = new
        return new

    def enqueue(self, job: Job) -> tuple[Job, bool]:
        with self._lock:
            key = (job.organization_id, job.idempotency_key)
            if key in self._keys:
                return self._jobs[self._keys[key]], False
            self._jobs[job.job_id] = job
            self._keys[key] = job.job_id
            return job, True

    def claim(self, worker_id: str, job_types: frozenset[str] | None = None) -> Job | None:
        with self._lock:
            now = self.clock()
            ready = [j for j in self._jobs.values() if j.status is JobStatus.QUEUED and j.available_at <= now
                     and (job_types is None or j.job_type in job_types)]
            if not ready:
                return None
            job = sorted(ready, key=lambda j: (-j.priority, j.available_at, j.created_at))[0]
            return self._save(job, status=JobStatus.CLAIMED, claimed_by=worker_id, attempts=job.attempts + 1,
                              lease_expires_at=now + timedelta(seconds=job.timeout_seconds))

    def _owned(self, job_id: str, worker_id: str) -> Job:
        job = self.get(job_id)
        if job.status is not JobStatus.CLAIMED or job.claimed_by != worker_id:
            raise InvalidTransitionError(f"job {job_id} is not claimed by {worker_id}")
        return job

    def ack(self, job_id: str, worker_id: str, result_ref: str | None = None) -> Job:
        with self._lock:
            job = self._owned(job_id, worker_id)
            return self._save(job, status=JobStatus.SUCCEEDED, result_ref=result_ref, lease_expires_at=None)

    def fail(self, job_id: str, worker_id: str, error: str, *, retryable: bool = False) -> Job:
        with self._lock:
            job = self._owned(job_id, worker_id)
            if self.rules.may_retry(job, retryable):
                return self._save(job, status=JobStatus.QUEUED, last_error=error, claimed_by=None,
                                  lease_expires_at=None, available_at=self.clock() + self.rules.backoff(job))
            return self._save(job, status=JobStatus.FAILED, last_error=error, lease_expires_at=None)

    def retry(self, job_id: str) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job.status is not JobStatus.FAILED:
                raise InvalidTransitionError("only FAILED jobs can be retried")
            if job.side_effect is SideEffect.EXTERNAL_WRITE:
                raise PolicyViolationError("EXTERNAL_WRITE jobs are never retried; request a new approval instead")
            if job.side_effect is SideEffect.INTERNAL and job.job_type not in self.rules.idempotent_job_types:
                raise PolicyViolationError(f"job type {job.job_type!r} is not declared idempotent")
            return self._save(job, status=JobStatus.QUEUED, claimed_by=None, available_at=self.clock(),
                              max_attempts=max(job.max_attempts, job.attempts + 1))

    def cancel(self, job_id: str, reason: str) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job.status is not JobStatus.QUEUED:
                raise InvalidTransitionError(f"only QUEUED jobs can be cancelled (job is {job.status.value})")
            return self._save(job, status=JobStatus.CANCELLED, last_error=f"cancelled: {reason}")

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"job {job_id} not found")
        return job

    def find_by_key(self, organization_id: str, idempotency_key: str) -> Job | None:
        jid = self._keys.get((organization_id, idempotency_key))
        return None if jid is None else self._jobs[jid]

    def reclaim_abandoned(self) -> list[Job]:
        out = []
        with self._lock:
            now = self.clock()
            for job in list(self._jobs.values()):
                if job.status is JobStatus.CLAIMED and job.lease_expires_at is not None and job.lease_expires_at <= now:
                    if self.rules.may_reclaim(job):
                        out.append(self._save(job, status=JobStatus.QUEUED, claimed_by=None, lease_expires_at=None,
                                              last_error="lease expired; reclaimed"))
                    else:
                        j = self._save(job, status=JobStatus.ABANDONED, lease_expires_at=None,
                                       last_error="lease expired; not safe to reclaim automatically")
                        out.append(j)
                        if self.on_abandoned:
                            self.on_abandoned(j)
        return out

    def list(self, organization_id: str, status: JobStatus | None = None, limit: int = 100) -> list[Job]:
        rows = [j for j in self._jobs.values() if j.organization_id == organization_id
                and (status is None or j.status is status)]
        return sorted(rows, key=lambda j: j.created_at)[:limit]


class TransientJobError(Exception):
    """Raised by a handler to signal a retryable failure (honoured only where the rules allow)."""


@dataclass
class JobWorker:
    queue: Any
    handlers: Mapping[str, Callable[[Job], str | None]]
    worker_id: str = field(default_factory=lambda: new_id("worker"))

    def run_once(self) -> Job | None:
        job = self.queue.claim(self.worker_id, frozenset(self.handlers))
        if job is None:
            return None
        try:
            ref = self.handlers[job.job_type](job)
        except TransientJobError as exc:
            return self.queue.fail(job.job_id, self.worker_id, f"transient: {exc}", retryable=True)
        except Exception as exc:
            return self.queue.fail(job.job_id, self.worker_id, f"{type(exc).__name__}: {exc}", retryable=False)
        return self.queue.ack(job.job_id, self.worker_id, ref)

    def drain(self, limit: int = 100) -> list[Job]:
        done = []
        for _ in range(limit):
            j = self.run_once()
            if j is None:
                break
            done.append(j)
        return done


# ---------------------------------------------------------------------------------------
# agent-run jobs
# ---------------------------------------------------------------------------------------
AGENT_RUN = "agent_run"


def agent_run_reclaim_policy(tenants: Any) -> Callable[[Job], bool]:
    """An abandoned agent-run job may be reclaimed only if its run never started."""
    def safe(job: Job) -> bool:
        if job.job_type != AGENT_RUN or not job.run_id:
            return False
        cc = tenants.tenants.get(job.organization_id)
        run = None if cc is None else cc.runs.repo.find(job.run_id)
        return run is not None and run.status.value == "QUEUED"
    return safe


def agent_run_handler(tenants: Any) -> Callable[[Job], str | None]:
    """Execute a queued agent run: rebuild the RunContext from the persisted run and payload."""
    from .agents import RunContext
    from .context import ExplicitContext

    def handle(job: Job) -> str | None:
        cc = tenants.tenants.get(job.organization_id)
        if cc is None or cc.runtime is None:
            raise RuntimeError("no runtime for the job's organization")
        run = cc.runs.get(job.run_id)
        p = job.payload
        ctx = RunContext("Command from an organization member", (), (), p["message"], run.principal,
                         conversation_id=p.get("conversation_id"), request_id=p.get("request_id"),
                         requested_agent_id=run.agent_id)
        explicit = [ExplicitContext(e["item_id"], e["title"], e["text"]) for e in p.get("explicit", [])]
        result = cc.runtime.execute(run.run_id, ctx, message=p["message"], explicit=explicit)
        return result.output_id or run.run_id
    return handle
