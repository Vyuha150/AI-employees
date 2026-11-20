"""Durable PostgreSQL job queue (survives restarts; safe concurrent claims via SKIP LOCKED)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable

from psycopg.types.json import Jsonb

from ..codec import decode, encode
from ..common import Clock, utc_now
from ..errors import ConcurrencyError, InvalidTransitionError, NotFoundError, PolicyViolationError
from ..jobs import Job, JobQueueRules, JobStatus, SideEffect
from .database import Database

_COLS = ("id, organization_id, job_type, idempotency_key, side_effect, run_id, workflow_run_id, priority, status, "
         "attempts, max_attempts, timeout_seconds, available_at, claimed_by, lease_expires_at, last_error, result_ref, "
         "record, version, created_at")


class PgJobQueue:
    durable = True

    def __init__(self, db: Database, rules: JobQueueRules | None = None, clock: Clock = utc_now,
                 on_abandoned: Callable[[Job], None] | None = None):
        self.db, self.rules, self.clock, self.on_abandoned = db, rules or JobQueueRules(), clock, on_abandoned
        self._org_cache: dict[str, str] = {}

    def _org(self, slug: str) -> str:
        if slug not in self._org_cache:
            with self.db.cursor() as cur:
                cur.execute("SELECT id FROM organizations WHERE slug = %s", (slug,))
                row = cur.fetchone()
            if row is None:
                raise NotFoundError(f"organization {slug!r} not found")
            self._org_cache[slug] = str(row[0])
        return self._org_cache[slug]

    @staticmethod
    def _decode(record: Any, version: int) -> Job:
        return replace(decode(Job, record), version=version)

    def _write(self, cur, job: Job, expected_version: int | None) -> Job:
        org = self._org(job.organization_id)
        vals = (job.job_type, job.idempotency_key, job.side_effect.value, job.run_id, job.workflow_run_id, job.priority,
                job.status.value, job.attempts, job.max_attempts, job.timeout_seconds, job.available_at, job.claimed_by,
                job.lease_expires_at, job.last_error, job.result_ref)
        if expected_version is None:
            cur.execute(f"INSERT INTO jobs ({_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (organization_id, idempotency_key) DO NOTHING RETURNING version",
                        (job.job_id, org, *vals, Jsonb(encode(job)), 0, job.created_at))
            return job if cur.fetchone() else None   # type: ignore[return-value]
        stored = replace(job, version=expected_version + 1)
        cur.execute("UPDATE jobs SET job_type=%s, idempotency_key=%s, side_effect=%s, run_id=%s, workflow_run_id=%s, "
                    "priority=%s, status=%s, attempts=%s, max_attempts=%s, timeout_seconds=%s, available_at=%s, "
                    "claimed_by=%s, lease_expires_at=%s, last_error=%s, result_ref=%s, record=%s, version=version+1 "
                    "WHERE id=%s AND version=%s RETURNING version", (*vals, Jsonb(encode(stored)), job.job_id,
                                                                    expected_version))
        if cur.fetchone() is None:
            raise ConcurrencyError(f"job {job.job_id} changed concurrently")
        return stored

    def enqueue(self, job: Job) -> tuple[Job, bool]:
        with self.db.transaction() as cur:
            if self._write(cur, job, None) is not None:
                return job, True
        existing = self.find_by_key(job.organization_id, job.idempotency_key)
        assert existing is not None
        return existing, False

    def claim(self, worker_id: str, job_types: frozenset[str] | None = None) -> Job | None:
        now = self.clock()
        with self.db.transaction() as cur:
            cur.execute("SELECT record, version FROM jobs WHERE status = 'QUEUED' AND available_at <= %s "
                        "AND (%s::text[] IS NULL OR job_type = ANY(%s::text[])) "
                        "ORDER BY priority DESC, available_at, created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                        (now, list(job_types) if job_types else None, list(job_types) if job_types else None))
            row = cur.fetchone()
            if row is None:
                return None
            job = self._decode(row[0], row[1])
            claimed = replace(job, status=JobStatus.CLAIMED, claimed_by=worker_id, attempts=job.attempts + 1,
                              lease_expires_at=now + timedelta(seconds=job.timeout_seconds))
            return self._write(cur, claimed, job.version)

    def _locked(self, cur, job_id: str) -> Job:
        cur.execute("SELECT record, version FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
        row = cur.fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id} not found")
        return self._decode(row[0], row[1])

    def _owned(self, cur, job_id: str, worker_id: str) -> Job:
        job = self._locked(cur, job_id)
        if job.status is not JobStatus.CLAIMED or job.claimed_by != worker_id:
            raise InvalidTransitionError(f"job {job_id} is not claimed by {worker_id}")
        return job

    def ack(self, job_id: str, worker_id: str, result_ref: str | None = None) -> Job:
        with self.db.transaction() as cur:
            job = self._owned(cur, job_id, worker_id)
            return self._write(cur, replace(job, status=JobStatus.SUCCEEDED, result_ref=result_ref,
                                            lease_expires_at=None), job.version)

    def fail(self, job_id: str, worker_id: str, error: str, *, retryable: bool = False) -> Job:
        with self.db.transaction() as cur:
            job = self._owned(cur, job_id, worker_id)
            if self.rules.may_retry(job, retryable):
                nxt = replace(job, status=JobStatus.QUEUED, last_error=error, claimed_by=None, lease_expires_at=None,
                              available_at=self.clock() + self.rules.backoff(job))
            else:
                nxt = replace(job, status=JobStatus.FAILED, last_error=error, lease_expires_at=None)
            return self._write(cur, nxt, job.version)

    def retry(self, job_id: str) -> Job:
        with self.db.transaction() as cur:
            job = self._locked(cur, job_id)
            if job.status is not JobStatus.FAILED:
                raise InvalidTransitionError("only FAILED jobs can be retried")
            if job.side_effect is SideEffect.EXTERNAL_WRITE:
                raise PolicyViolationError("EXTERNAL_WRITE jobs are never retried; request a new approval instead")
            if job.side_effect is SideEffect.INTERNAL and job.job_type not in self.rules.idempotent_job_types:
                raise PolicyViolationError(f"job type {job.job_type!r} is not declared idempotent")
            return self._write(cur, replace(job, status=JobStatus.QUEUED, claimed_by=None, available_at=self.clock(),
                                            max_attempts=max(job.max_attempts, job.attempts + 1)), job.version)

    def cancel(self, job_id: str, reason: str) -> Job:
        with self.db.transaction() as cur:
            job = self._locked(cur, job_id)
            if job.status is not JobStatus.QUEUED:
                raise InvalidTransitionError(f"only QUEUED jobs can be cancelled (job is {job.status.value})")
            return self._write(cur, replace(job, status=JobStatus.CANCELLED, last_error=f"cancelled: {reason}"),
                               job.version)

    def get(self, job_id: str) -> Job:
        with self.db.cursor() as cur:
            cur.execute("SELECT record, version FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id} not found")
        return self._decode(row[0], row[1])

    def find_by_key(self, organization_id: str, idempotency_key: str) -> Job | None:
        with self.db.cursor() as cur:
            cur.execute("SELECT record, version FROM jobs WHERE organization_id = %s AND idempotency_key = %s",
                        (self._org(organization_id), idempotency_key))
            row = cur.fetchone()
        return None if row is None else self._decode(row[0], row[1])

    def reclaim_abandoned(self) -> list[Job]:
        out = []
        with self.db.transaction() as cur:
            cur.execute("SELECT record, version FROM jobs WHERE status = 'CLAIMED' AND lease_expires_at <= %s "
                        "FOR UPDATE SKIP LOCKED", (self.clock(),))
            for rec, ver in cur.fetchall():
                job = self._decode(rec, ver)
                if self.rules.may_reclaim(job):
                    out.append(self._write(cur, replace(job, status=JobStatus.QUEUED, claimed_by=None,
                                                        lease_expires_at=None, last_error="lease expired; reclaimed"),
                                           job.version))
                else:
                    out.append(self._write(cur, replace(job, status=JobStatus.ABANDONED, lease_expires_at=None,
                                                        last_error="lease expired; not safe to reclaim automatically"),
                                           job.version))
        for j in out:
            if j.status is JobStatus.ABANDONED and self.on_abandoned:
                self.on_abandoned(j)
        return out

    def list(self, organization_id: str, status: JobStatus | None = None, limit: int = 100) -> list[Job]:
        with self.db.cursor() as cur:
            cur.execute("SELECT record, version FROM jobs WHERE organization_id = %s AND (%s::text IS NULL OR status = %s) "
                        "ORDER BY created_at LIMIT %s",
                        (self._org(organization_id), getattr(status, "value", None), getattr(status, "value", None),
                         limit))
            return [self._decode(r[0], r[1]) for r in cur.fetchall()]
