"""Scheduler (Parts 37-39).

Jobs come from agent profiles (schedule_policy.jobs). Each job has a cron expression evaluated
in its IANA timezone (ORGANIZATION_DEFAULT -> organization policy timezone; nothing is hardcoded).

tick(now):
  occurrences since the job's watermark are split into
    current: within MISSED_GRACE of now -> triggered
    missed:  older                     -> handled by the job's missed_run_policy
      SKIP      (default) record each as SKIPPED
      RUN_ONCE  trigger only the most recent missed occurrence
      CATCH_UP  trigger missed occurrences oldest-first, at most scheduler.max_catch_up_runs
  occurrences older than scheduler.lookback_hours are never run.
  concurrency FORBID (default): skip while the job's previous run is still active.
  Every trigger claims an idempotency key (organization, "schedule", job_id, scheduled_for):
  a duplicate tick or a second scheduler instance cannot start the same occurrence twice.
A job whose agent is disabled is disabled. The scheduler only dispatches; what runs is a
workflow subject to the normal gateway, approvals and budgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable

from .agents import AgentRegistry, ConcurrencyPolicy, MissedRunPolicy
from .audit import AuditLogger, EventType
from .common import ActorType, Clock, StrEnum, new_id, utc_now
from .cron import CronExpression
from .errors import ConfigurationError
from .idempotency import IdempotencyStore, InMemoryIdempotencyStore, idempotency_key
from .policy import OrganizationPolicy
from .storage import InMemoryRepository, Repository

MISSED_GRACE = timedelta(minutes=15)


class ScheduledRunStatus(StrEnum):
    TRIGGERED = "TRIGGERED"
    SKIPPED = "SKIPPED"
    DUPLICATE = "DUPLICATE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ScheduledJob:
    job_id: str
    organization_id: str
    agent_id: str
    workflow: str
    cron: str
    timezone: str
    enabled: bool
    missed_run_policy: MissedRunPolicy
    concurrency_policy: ConcurrencyPolicy
    watermark: datetime                     # occurrences at or before this are handled
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    version: int = 0


@dataclass(frozen=True)
class ScheduledRun:
    scheduled_run_id: str
    organization_id: str
    job_id: str
    workflow: str
    scheduled_for: datetime
    decided_at: datetime
    status: ScheduledRunStatus
    idempotency_key: str
    reason: str
    workflow_ref: str | None = None
    version: int = 0


Dispatcher = Callable[[ScheduledJob, datetime], str | None]   # returns a run/workflow reference


@dataclass
class Scheduler:
    policy: OrganizationPolicy
    registry: AgentRegistry
    audit: AuditLogger
    dispatcher: Dispatcher
    idempotency: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    jobs: Repository[ScheduledJob] = field(default_factory=lambda: InMemoryRepository("job_id"))
    runs: Repository[ScheduledRun] = field(default_factory=lambda: InMemoryRepository("scheduled_run_id"))
    is_active: Callable[[ScheduledJob], bool] = lambda job: False
    clock: Clock = utc_now

    def sync_jobs(self) -> list[ScheduledJob]:
        """Create/refresh jobs from agent profiles. New jobs start at 'now' (no backfill)."""
        now = self.clock()
        out = []
        for agent in self.registry.list_agents(include_disabled=True):
            for spec in agent.schedule_jobs:
                tz = self.policy.timezone if spec.timezone == "ORGANIZATION_DEFAULT" else spec.timezone
                cron = CronExpression.parse(spec.cron)
                enabled = spec.enabled and agent.enabled and self.policy.scheduler.enabled
                existing = self.jobs.find(spec.job_id)
                if existing is not None and existing.agent_id != agent.agent_id:
                    raise ConfigurationError(f"job_id {spec.job_id!r} is defined by two agents")
                job = ScheduledJob(spec.job_id, self.policy.organization_id, agent.agent_id, spec.workflow, spec.cron, tz,
                                   enabled, spec.missed_run_policy, spec.concurrency_policy,
                                   existing.watermark if existing else now, cron.next_after(now, tz),
                                   existing.last_run_at if existing else None)
                if existing is None:
                    self.jobs.add(job)
                else:
                    job = self.jobs.update(replace(job, version=existing.version), expected_version=existing.version)
                out.append(job)
        return out

    def _record(self, job: ScheduledJob, when: datetime, status: ScheduledRunStatus, key: str, reason: str,
                ref: str | None = None) -> ScheduledRun:
        r = ScheduledRun(new_id("srun"), job.organization_id, job.job_id, job.workflow, when, self.clock(), status, key,
                         reason, ref)
        self.runs.add(r)
        self.audit.record(EventType.SCHEDULE_TRIGGERED if status is ScheduledRunStatus.TRIGGERED else EventType.SCHEDULE_SKIPPED,
                          organization_id=job.organization_id, actor_type=ActorType.SYSTEM, actor_id="scheduler",
                          agent_id=job.agent_id, object_type="scheduled_job", object_id=job.job_id,
                          payload={"scheduled_for": when.isoformat(), "status": status.value, "reason": reason,
                                   "workflow": job.workflow, "ref": ref})
        return r

    def _trigger(self, job: ScheduledJob, when: datetime, reason: str) -> ScheduledRun:
        key = idempotency_key(job.organization_id, "schedule", job.job_id, when.isoformat())
        fresh, rec = self.idempotency.claim(job.organization_id, key)
        if not fresh:
            return self._record(job, when, ScheduledRunStatus.DUPLICATE, key, "already triggered", rec.result_ref)
        if job.concurrency_policy is ConcurrencyPolicy.FORBID and self.is_active(job):
            return self._record(job, when, ScheduledRunStatus.SKIPPED, key, "previous run still active (FORBID)")
        try:
            ref = self.dispatcher(job, when)
        except Exception as exc:   # dispatch failure is recorded; the key stays claimed (no duplicate retry)
            return self._record(job, when, ScheduledRunStatus.FAILED, key, f"dispatch failed: {type(exc).__name__}: {exc}")
        if ref:
            self.idempotency.set_result(job.organization_id, key, ref)
        return self._record(job, when, ScheduledRunStatus.TRIGGERED, key, reason, ref)

    def tick(self, now: datetime | None = None) -> list[ScheduledRun]:
        now = now or self.clock()
        out: list[ScheduledRun] = []
        lookback = now - timedelta(hours=self.policy.scheduler.lookback_hours)
        for job in sorted(self.jobs.list(lambda j: j.organization_id == self.policy.organization_id),
                          key=lambda j: j.job_id):
            cron = CronExpression.parse(job.cron)
            if not job.enabled:
                self.jobs.update(replace(job, watermark=now, next_run_at=None), expected_version=job.version)
                continue
            occ = cron.occurrences(max(job.watermark, lookback - timedelta(seconds=1)), now, job.timezone)
            too_old = cron.occurrences(job.watermark, lookback, job.timezone) if job.watermark < lookback else []
            for when in too_old:
                out.append(self._record(job, when, ScheduledRunStatus.SKIPPED,
                                        idempotency_key(job.organization_id, "schedule", job.job_id, when.isoformat()),
                                        "older than scheduler.lookback_hours"))
            current = [t for t in occ if now - t <= MISSED_GRACE]
            missed = [t for t in occ if now - t > MISSED_GRACE]
            to_run: list[tuple[datetime, str]] = []
            policy = job.missed_run_policy
            if missed:
                if policy is MissedRunPolicy.RUN_ONCE and not current:
                    to_run.append((missed[-1], "RUN_ONCE: most recent missed occurrence"))
                    skipped = missed[:-1]
                elif policy is MissedRunPolicy.CATCH_UP:
                    cap = self.policy.scheduler.max_catch_up_runs
                    to_run += [(t, "CATCH_UP") for t in missed[:cap]]
                    skipped = missed[cap:]
                else:
                    skipped = missed
                for t in skipped:
                    out.append(self._record(job, t, ScheduledRunStatus.SKIPPED,
                                            idempotency_key(job.organization_id, "schedule", job.job_id, t.isoformat()),
                                            f"missed ({policy.value})"))
            to_run += [(t, "on schedule") for t in current]
            last = job.last_run_at
            for t, why in to_run:
                r = self._trigger(job, t, why)
                out.append(r)
                if r.status is ScheduledRunStatus.TRIGGERED:
                    last = t
            fresh = self.jobs.get(job.job_id)
            self.jobs.update(replace(fresh, watermark=max([fresh.watermark, now]), last_run_at=last,
                                     next_run_at=cron.next_after(now, job.timezone)), expected_version=fresh.version)
        return out
