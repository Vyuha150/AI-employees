"""PostgreSQL repositories implementing storage.Repository and the audit/idempotency contracts.

Tenant isolation at the adapter: every repository is bound to ONE organization (uuid + slug).
Every statement filters on organization_id; writing a record whose organization_id differs
from the bound organization raises PermissionDeniedError. RLS (docs/supabase_rls.md) is a
second, independent layer for any client that reaches the database directly.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Generic, Iterable, Mapping, TypeVar

from psycopg.types.json import Jsonb

from ..audit import AuditEvent, GENESIS_HASH, _chain_hash, _matches, verify_events
from ..canonical import canonical_hash
from ..codec import decode, encode
from ..errors import AuditIntegrityError, ConcurrencyError, NotFoundError, PermissionDeniedError
from ..idempotency import IdempotencyRecord
from .database import Database

T = TypeVar("T")


def _uuid_or_none(v: Any) -> str | None:
    if v is None:
        return None
    try:
        return str(uuid.UUID(str(v)))
    except ValueError:
        return None


@dataclass(frozen=True)
class TableSpec:
    table: str
    record_cls: type
    id_field: str
    columns: Callable[[Any], Mapping[str, Any]]
    key_column: str = "id"
    order_by: str = "created_at"
    after_insert: Callable[[Any, Any, str], None] | None = None     # (cursor, record, org_uuid)


class PgRepository(Generic[T]):
    def __init__(self, db: Database, org_uuid: str, org_slug: str, spec: TableSpec):
        self.db, self.org_uuid, self.org_slug, self.spec = db, org_uuid, org_slug, spec

    def _check(self, rec: Any) -> None:
        if getattr(rec, "organization_id", None) != self.org_slug:
            raise PermissionDeniedError("record belongs to a different organization than this repository")

    def _row(self, rec: Any) -> dict[str, Any]:
        data = encode(rec)
        cols = {k: (Jsonb(v) if isinstance(v, (dict, list)) and k != "permissions" else v)
                for k, v in self.spec.columns(rec).items()}
        return {"record": Jsonb(data), **cols}

    def _decode(self, record: Mapping[str, Any], version: int) -> T:
        return replace(decode(self.spec.record_cls, record), version=version)

    def add(self, rec: T) -> T:
        self._check(rec)
        key = getattr(rec, self.spec.id_field)
        row = self._row(rec)
        cols = ["organization_id", self.spec.key_column, "version", *row]
        vals = [self.org_uuid, key, getattr(rec, "version", 0), *row.values()]
        sql = (f"INSERT INTO {self.spec.table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
               f"ON CONFLICT DO NOTHING RETURNING 1")
        with self.db.transaction() as cur:
            cur.execute(sql, vals)
            if cur.fetchone() is None:
                raise ConcurrencyError(f"{self.spec.table}: {key!r} already exists")
            if self.spec.after_insert:
                self.spec.after_insert(cur, rec, self.org_uuid)
        return rec

    def find(self, record_id: str) -> T | None:
        if self.spec.key_column == "id" and _uuid_or_none(record_id) is None:
            return None
        with self.db.cursor() as cur:
            cur.execute(f"SELECT record, version FROM {self.spec.table} WHERE organization_id = %s AND "
                        f"{self.spec.key_column} = %s", (self.org_uuid, record_id))
            row = cur.fetchone()
        return None if row is None else self._decode(row[0], row[1])

    def get(self, record_id: str) -> T:
        found = self.find(record_id)
        if found is None:
            raise NotFoundError(f"{self.spec.table}: {record_id!r} not found")
        return found

    def update(self, rec: T, expected_version: int) -> T:
        self._check(rec)
        key = getattr(rec, self.spec.id_field)
        stored = replace(rec, version=expected_version + 1)
        row = self._row(stored)
        sets = ", ".join(f"{c} = %s" for c in row)
        sql = (f"UPDATE {self.spec.table} SET {sets}, version = version + 1 WHERE organization_id = %s AND "
               f"{self.spec.key_column} = %s AND version = %s RETURNING version")
        with self.db.transaction() as cur:
            cur.execute(sql, [*row.values(), self.org_uuid, key, expected_version])
            if cur.fetchone() is None:
                cur.execute(f"SELECT version FROM {self.spec.table} WHERE organization_id = %s AND "
                            f"{self.spec.key_column} = %s", (self.org_uuid, key))
                found = cur.fetchone()
                if found is None:
                    raise NotFoundError(f"{self.spec.table}: {key!r} not found")
                raise ConcurrencyError(f"{self.spec.table}: {key!r} expected version {expected_version}, found {found[0]}")
        return stored

    def list(self, predicate: Callable[[T], bool] | None = None) -> list[T]:
        with self.db.cursor() as cur:
            cur.execute(f"SELECT record, version FROM {self.spec.table} WHERE organization_id = %s "
                        f"ORDER BY {self.spec.order_by}", (self.org_uuid,))
            rows = cur.fetchall()
        items = [self._decode(r[0], r[1]) for r in rows]
        return [i for i in items if predicate is None or predicate(i)]


# ---------------------------------------------------------------------------------------
# table specs
# ---------------------------------------------------------------------------------------
def _task_deps(cur, task, org_uuid):
    for dep in task.dependencies:
        cur.execute("INSERT INTO task_dependencies (organization_id, task_id, depends_on) VALUES (%s, %s, %s)",
                    (org_uuid, task.task_id, dep))


def _workflow_steps(cur, d, org_uuid):
    for s in d.steps:
        cur.execute("INSERT INTO workflow_steps (organization_id, workflow_id, step_id, step_type, config, next_step) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (org_uuid, d.workflow_id, s.step_id, s.type.value, Jsonb(encode(s.config)), s.next))


def specs() -> dict[str, TableSpec]:
    from ..alerts import Alert, Notification
    from ..approvals import Approval
    from ..kpis import KpiSnapshot
    from ..knowledge import KnowledgeChunk, KnowledgeDocument
    from ..memory import Memory
    from ..orchestration import Delegation
    from ..outputs import AgentOutput
    from ..recommendations import ChangeRequest, Recommendation
    from ..runs import AgentRun
    from ..scheduler import ScheduledJob, ScheduledRun
    from ..tasks import Task
    from ..usage import UsageRecord
    from ..workflows import WorkflowDefinition, WorkflowRun

    v = lambda e: None if e is None else getattr(e, "value", e)
    return {
        "runs": TableSpec("agent_runs", AgentRun, "run_id", lambda r: {
            "agent_key": r.agent_id, "status": r.status.value, "triggered_by": r.triggered_by,
            "parent_run_id": r.parent_run_id, "depth": r.depth, "retry_of": r.retry_of, "tokens_used": r.tokens_used,
            "cost_used": r.cost_used, "model_provider": r.model_provider, "model_name": r.model_name,
            "fallback_used": r.fallback_used, "created_at": r.created_at}),
        "tasks": TableSpec("tasks", Task, "task_id", lambda t: {
            "title": t.title, "owner": t.owner, "owner_type": t.owner_type.value, "priority": t.priority.value,
            "status": t.status.value, "risk_level": t.risk_level.value, "due_date": t.due_date,
            "source_run_id": _uuid_or_none(t.source_run_id), "created_at": t.created_at}, after_insert=_task_deps),
        "approvals": TableSpec("approvals", Approval, "approval_id", lambda a: {
            "run_id": a.run_id, "agent_key": a.agent_id, "tool_name": a.tool_name, "action_type": a.action_type,
            "payload_hash": a.payload_hash, "risk_class": a.risk_class.value, "required_role": a.required_role,
            "status": a.status.value, "execution_status": a.execution_status.value, "requested_by": a.requested_by,
            "approved_by": a.approved_by, "requested_at": a.requested_at, "expires_at": a.expires_at},
            order_by="requested_at"),
        "outputs": TableSpec("outputs", AgentOutput, "output_id", lambda o: {
            "run_id": o.run_id, "agent_key": o.agent_id, "kind": o.kind.value, "validity": o.validity.value,
            "content_hash": o.content_hash, "ingested": o.ingested, "created_at": o.created_at}),
        "recommendations": TableSpec("recommendations", Recommendation, "recommendation_id", lambda r: {
            "source_agent": r.source_agent, "run_id": _uuid_or_none(r.run_id), "status": r.status.value,
            "impact_category": r.impact_category.value, "confidence": r.confidence.value, "fingerprint": r.fingerprint,
            "decided_by": r.decided_by, "created_at": r.created_at}),
        "changes": TableSpec("change_requests", ChangeRequest, "change_id", lambda c: {
            "recommendation_id": c.recommendation_id, "status": c.status.value, "owner": c.owner,
            "created_at": c.created_at}),
        "memories": TableSpec("memories", Memory, "memory_id", lambda m: {
            "scope": m.scope.value, "scope_ref": m.scope_ref, "status": m.status.value,
            "proposed_by_agent": m.proposed_by_agent, "decided_by": m.decided_by, "review_at": m.review_at,
            "expires_at": m.expires_at, "created_at": m.proposed_at}),
        "delegations": TableSpec("delegations", Delegation, "delegation_id", lambda d: {
            "parent_run_id": d.parent_run_id, "child_run_id": d.child_run_id, "requested_by_agent": d.requested_by_agent,
            "target_agent": d.target_agent_id, "status": d.status.value, "created_at": d.created_at}),
        "alerts": TableSpec("alerts", Alert, "alert_id", lambda a: {
            "severity": a.severity.value, "category": a.category, "title": a.title, "status": a.status.value,
            "dedupe_key": a.dedupe_key, "created_at": a.created_at}),
        "notifications": TableSpec("notifications", Notification, "notification_id", lambda n: {
            "recipient": n.recipient, "channel": n.channel, "alert_id": n.alert_id, "status": n.status.value,
            "created_at": n.created_at}),
        "kpis": TableSpec("kpi_snapshots", KpiSnapshot, "snapshot_id", lambda s: {
            "kpi_key": s.kpi, "value": s.value, "unit": s.unit, "observed_at": s.observed_at,
            "source_reference": s.source_reference, "recorded_by": s.recorded_by, "run_id": _uuid_or_none(s.run_id),
            "created_at": s.recorded_at}),
        "usage": TableSpec("ai_usage", UsageRecord, "usage_id", lambda u: {
            "run_id": u.run_id, "agent_key": u.agent_id, "provider": u.provider, "model": u.model,
            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "cache_read_tokens": u.cache_read_tokens,
            "cache_write_tokens": u.cache_write_tokens, "tool_calls": u.tool_calls, "estimated_cost": u.estimated_cost,
            "cost_explanation": u.cost_explanation, "currency": u.currency, "fallback_used": u.fallback_used,
            "created_at": u.timestamp}),
        "knowledge_documents": TableSpec("knowledge_documents", KnowledgeDocument, "document_id", lambda d: {
            "title": d.title, "category": d.category, "permissions": list(d.permissions),
            "source_reference": d.source_reference, "valid_from": d.valid_from, "valid_until": d.valid_until,
            "created_at": d.created_at}),
        "knowledge_chunks": TableSpec("knowledge_chunks", KnowledgeChunk, "chunk_id", lambda c: {
            "document_id": c.document_id, "ordinal": c.ordinal, "text": c.text,
            "embedding": None if c.embedding is None else list(c.embedding)}, order_by="document_id, ordinal"),
        "workflows": TableSpec("workflows", WorkflowDefinition, "workflow_id", lambda d: {
            "name": d.name, "definition_version": d.definition_version, "status": d.status.value,
            "created_by": d.created_by}, after_insert=_workflow_steps),
        "workflow_runs": TableSpec("workflow_runs", WorkflowRun, "workflow_run_id", lambda r: {
            "workflow_id": r.workflow_id, "status": r.status.value, "current_step": r.current_step,
            "created_at": r.started_at}),
        "scheduled_jobs": TableSpec("agent_schedules", ScheduledJob, "job_id", lambda j: {
            "agent_key": j.agent_id, "workflow": j.workflow, "cron": j.cron, "timezone": j.timezone,
            "enabled": j.enabled, "missed_run_policy": v(j.missed_run_policy), "concurrency_policy": v(j.concurrency_policy),
            "watermark": j.watermark, "next_run_at": j.next_run_at, "last_run_at": j.last_run_at},
            key_column="job_id"),
        "scheduled_runs": TableSpec("scheduled_runs", ScheduledRun, "scheduled_run_id", lambda r: {
            "job_id": r.job_id, "scheduled_for": r.scheduled_for, "status": r.status.value,
            "idempotency_key": r.idempotency_key}),
    }


class PgRepositories:
    """All entity repositories for one organization; pass as CommandCenter.build(repositories=...)."""

    def __init__(self, db: Database, org_uuid: str, org_slug: str):
        self.db, self.org_uuid, self.org_slug = db, org_uuid, org_slug
        for name, spec in specs().items():
            setattr(self, name, PgRepository(db, org_uuid, org_slug, spec))
        self.audit = PgAuditStore(db, org_uuid, org_slug)
        self.idempotency = PgIdempotencyStore(db, org_uuid)
        self.agents = AgentRepository(db, org_uuid, org_slug)
        self.evaluations = EvaluationRepository(db, org_uuid)
        self.sources = SourceRepository(db, org_uuid)
        self.tool_calls = ToolCallRepository(db, org_uuid)


# ---------------------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------------------
class PgAuditStore:
    """Append-only, per-organization hash chain. Appends serialize on an advisory lock."""

    def __init__(self, db: Database, org_uuid: str, org_slug: str):
        self.db, self.org_uuid, self.org_slug = db, org_uuid, org_slug

    def append(self, event: AuditEvent) -> AuditEvent:
        if event.sequence is not None or event.event_hash is not None:
            raise AuditIntegrityError("events are sealed by the store; do not pre-set sequence/hash")
        if event.organization_id != self.org_slug:
            raise PermissionDeniedError("audit event for a different organization")
        with self.db.transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"audit:{self.org_uuid}",))
            cur.execute("SELECT org_sequence, event_hash FROM audit_events WHERE organization_id = %s "
                        "ORDER BY org_sequence DESC LIMIT 1", (self.org_uuid,))
            last = cur.fetchone()
            seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS_HASH)
            staged = AuditEvent(**{**event.__dict__, "sequence": seq})
            sealed = AuditEvent(**{**staged.__dict__, "prev_hash": prev, "event_hash": _chain_hash(staged, prev)})
            cur.execute(
                "INSERT INTO audit_events (id, organization_id, org_sequence, occurred_at, actor_type, actor_id, "
                "agent_key, run_id, event_type, object_type, object_id, payload, previous_state, new_state, prev_hash, "
                "event_hash, body) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_uuid_or_none(sealed.event_id) or str(uuid.uuid4()), self.org_uuid, seq, sealed.timestamp,
                 sealed.actor_type.value, sealed.actor_id, sealed.agent_id, _uuid_or_none(sealed.run_id),
                 sealed.event_type.value, sealed.object_type, sealed.object_id,
                 Jsonb(json.loads(sealed.payload_json)) if sealed.payload_json else None,
                 sealed.previous_state, sealed.new_state, prev, sealed.event_hash, Jsonb(sealed.to_record())))
        return sealed

    def _load(self, where: str = "", params: Iterable[Any] = ()) -> list[AuditEvent]:
        with self.db.cursor() as cur:
            cur.execute(f"SELECT body FROM audit_events WHERE organization_id = %s {where} ORDER BY org_sequence",
                        (self.org_uuid, *params))
            return [AuditEvent.from_record(r[0]) for r in cur.fetchall()]

    def events(self, **filters: Any) -> list[AuditEvent]:
        where, params = "", []
        col = {"event_type": "event_type", "object_type": "object_type", "object_id": "object_id"}
        for k, c in col.items():
            v = filters.get(k)
            if v is not None and not isinstance(v, (list, tuple, set, frozenset)):
                where += f" AND {c} = %s"
                params.append(getattr(v, "value", v))
        return [e for e in self._load(where, params) if _matches(e, filters)]

    def verify_chain(self) -> None:
        verify_events(self._load())


class PgIdempotencyStore:
    def __init__(self, db: Database, org_uuid: str):
        self.db, self.org_uuid = db, org_uuid

    def claim(self, organization_id: str, key: str, result_ref: str | None = None):
        with self.db.transaction() as cur:
            cur.execute("INSERT INTO idempotency_keys (organization_id, key, result_ref) VALUES (%s, %s, %s) "
                        "ON CONFLICT DO NOTHING RETURNING created_at", (self.org_uuid, key, result_ref))
            row = cur.fetchone()
            if row is not None:
                return True, IdempotencyRecord(organization_id, key, row[0], result_ref)
            cur.execute("SELECT created_at, result_ref FROM idempotency_keys WHERE organization_id = %s AND key = %s",
                        (self.org_uuid, key))
            existing = cur.fetchone()
            return False, IdempotencyRecord(organization_id, key, existing[0], existing[1])

    def get(self, organization_id: str, key: str):
        with self.db.cursor() as cur:
            cur.execute("SELECT created_at, result_ref FROM idempotency_keys WHERE organization_id = %s AND key = %s",
                        (self.org_uuid, key))
            row = cur.fetchone()
        return None if row is None else IdempotencyRecord(organization_id, key, row[0], row[1])

    def set_result(self, organization_id: str, key: str, result_ref: str) -> None:
        with self.db.cursor() as cur:
            cur.execute("UPDATE idempotency_keys SET result_ref = %s WHERE organization_id = %s AND key = %s",
                        (result_ref, self.org_uuid, key))


# ---------------------------------------------------------------------------------------
# agents, evaluations, sources, tool calls
# ---------------------------------------------------------------------------------------
class AgentRepository:
    """Mirrors the validated registry into agents / agent_versions / agent_tools / kpis.
    Each distinct profile content becomes a new agent_versions row (history is kept)."""

    def __init__(self, db: Database, org_uuid: str, org_slug: str):
        self.db, self.org_uuid, self.org_slug = db, org_uuid, org_slug

    def sync(self, registry: Any) -> list[str]:
        changed = []
        profiles = registry.list_agents(include_disabled=True)
        with self.db.transaction() as cur:
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
            for p in profiles:
                profile = encode(p)
                h = canonical_hash(profile)
                cur.execute("INSERT INTO agents (organization_id, agent_key, role, name, enabled, reports_to) "
                            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (organization_id, agent_key) DO UPDATE SET "
                            "role = EXCLUDED.role, name = EXCLUDED.name, enabled = EXCLUDED.enabled, "
                            "reports_to = EXCLUDED.reports_to",
                            (self.org_uuid, p.agent_id, p.role.value, p.name, p.enabled, p.reports_to))
                cur.execute("INSERT INTO agent_versions (organization_id, agent_key, profile_hash, profile) "
                            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                            (self.org_uuid, p.agent_id, h, Jsonb(profile)))
                new = cur.fetchone()
                if new:
                    changed.append(p.agent_id)
                    cur.execute("UPDATE agents SET current_version_id = %s WHERE organization_id = %s AND agent_key = %s",
                                (new[0], self.org_uuid, p.agent_id))
                cur.execute("DELETE FROM agent_tools WHERE organization_id = %s AND agent_key = %s",
                            (self.org_uuid, p.agent_id))
                for rel, names in (("ALLOWED", p.allowed_tools), ("PROHIBITED", p.prohibited_actions),
                                   ("ALWAYS_REQUIRE_APPROVAL", p.always_require_approval)):
                    for t in names:
                        cur.execute("INSERT INTO agent_tools VALUES (%s,%s,%s,%s)", (self.org_uuid, p.agent_id, t, rel))
                for k in p.kpis:
                    cur.execute("INSERT INTO kpis (organization_id, kpi_key, owner_agent, definition, direction, unit) "
                                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (organization_id, kpi_key, owner_agent) DO "
                                "UPDATE SET definition = EXCLUDED.definition, direction = EXCLUDED.direction, "
                                "unit = EXCLUDED.unit",
                                (self.org_uuid, k.name, p.agent_id, k.definition, k.direction.value, k.unit))
        return changed

    def list(self) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute("SELECT agent_key, role, name, enabled, reports_to FROM agents WHERE organization_id = %s "
                        "ORDER BY agent_key", (self.org_uuid,))
            return [dict(zip(("agent_id", "role", "name", "enabled", "reports_to"), r)) for r in cur.fetchall()]

    def versions(self, agent_id: str) -> list[str]:
        with self.db.cursor() as cur:
            cur.execute("SELECT profile_hash FROM agent_versions WHERE organization_id = %s AND agent_key = %s "
                        "ORDER BY created_at", (self.org_uuid, agent_id))
            return [r[0] for r in cur.fetchall()]


class EvaluationRepository:
    def __init__(self, db: Database, org_uuid: str):
        self.db, self.org_uuid = db, org_uuid

    def add(self, ev: Any) -> None:
        with self.db.cursor() as cur:
            cur.execute("INSERT INTO evaluations (organization_id, output_id, reviewer, accuracy, relevance, completeness,"
                        " actionability, business_value, composite_score, decision, comment, reviewed_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (self.org_uuid, ev.output_id, ev.reviewer, ev.accuracy, ev.relevance, ev.completeness,
                         ev.actionability, ev.business_value, ev.composite_score, ev.decision.value, ev.comment,
                         ev.reviewed_at))

    def list(self, output_id: str) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute("SELECT reviewer, composite_score, decision FROM evaluations WHERE organization_id = %s AND "
                        "output_id = %s ORDER BY reviewed_at", (self.org_uuid, output_id))
            return [{"reviewer": r[0], "composite_score": str(r[1]), "decision": r[2]} for r in cur.fetchall()]


class SourceRepository:
    def __init__(self, db: Database, org_uuid: str):
        self.db, self.org_uuid = db, org_uuid

    def add(self, source: Any, run_id: str | None = None) -> None:
        with self.db.cursor() as cur:
            cur.execute("INSERT INTO sources (organization_id, source_key, source_type, title, reference, retrieved_at, "
                        "authority_level, run_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (self.org_uuid, source.source_id, source.source_type.value, source.title, source.reference,
                         source.retrieved_at, getattr(source.authority_level, "value", None), _uuid_or_none(run_id)))

    def list(self, run_id: str | None = None) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute("SELECT source_key, source_type, reference FROM sources WHERE organization_id = %s "
                        "AND (%s::uuid IS NULL OR run_id = %s::uuid) ORDER BY created_at",
                        (self.org_uuid, run_id, run_id))
            return [{"source_id": r[0], "source_type": r[1], "reference": r[2]} for r in cur.fetchall()]


class ToolCallRepository:
    """Read side of tool_calls (rows are written by the audit routing trigger)."""

    def __init__(self, db: Database, org_uuid: str):
        self.db, self.org_uuid = db, org_uuid

    def list(self, run_id: str | None = None) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute("SELECT tool_name, status, risk_class, approval_id, execution_id, mock, run_id FROM tool_calls "
                        "WHERE organization_id = %s AND (%s::uuid IS NULL OR run_id = %s::uuid) ORDER BY executed_at",
                        (self.org_uuid, run_id, run_id))
            keys = ("tool_name", "status", "risk_class", "approval_id", "execution_id", "mock", "run_id")
            return [{k: (str(v) if k in ("approval_id", "run_id") and v else v) for k, v in zip(keys, r)}
                    for r in cur.fetchall()]
