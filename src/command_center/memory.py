"""Memory governance (Part 17).

Candidates are PROPOSED, never auto-persisted as context. Only a human can ACCEPT or
REJECT. `context_for()` returns ACCEPTED, unexpired memories within the agent's
memory_scopes, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping

from .agents import AgentProfile, MemoryScope
from .audit import AuditLogger, EventType
from .common import ActorType, Clock, Confidence, Principal, new_id, utc_now
from .errors import PermissionDeniedError, ValidationFailedError
from .sources import SourceCatalog
from .state_machines import MEMORY_MACHINE, MemoryStatus
from .storage import InMemoryRepository, Repository


@dataclass(frozen=True)
class Memory:
    memory_id: str
    organization_id: str
    fact: str
    scope: MemoryScope
    scope_ref: str | None
    source_ids: tuple[str, ...]
    source_refs: tuple[str, ...]      # resolved source references, so the memory stays traceable
    confidence: Confidence
    reason_to_remember: str
    proposed_by_agent: str
    proposed_in_run: str | None
    proposed_at: datetime
    category: str = "OTHER"
    review_at: datetime | None = None
    expires_at: datetime | None = None
    status: MemoryStatus = MemoryStatus.PROPOSED
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    version: int = 0


def _parse_dt(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    dt = datetime.fromisoformat(str(v))
    if dt.tzinfo is None:
        raise ValidationFailedError("memory datetimes must be timezone-aware")
    return dt


@dataclass
class MemoryService:
    audit: AuditLogger
    repo: Repository[Memory] = field(default_factory=lambda: InMemoryRepository("memory_id"))
    clock: Clock = utc_now

    def propose(self, candidate: Mapping[str, Any], *, agent: AgentProfile, organization_id: str,
                run_id: str | None, sources: SourceCatalog) -> Memory:
        """Accepts the wire shape (content/rationale) or the model shape (fact/reason_to_remember)."""
        fact = candidate.get("fact") or candidate.get("content")
        reason = candidate.get("reason_to_remember") or candidate.get("rationale")
        if not fact or not reason:
            raise ValidationFailedError("memory candidate needs a fact and a reason to remember")
        source_ids = list(candidate.get("source_ids", []))
        sources.require(source_ids, "memory candidate")

        scope = MemoryScope(candidate.get("scope", MemoryScope.AGENT.value))  # narrowest scope by default
        scope_ref = candidate.get("scope_ref")
        if scope is MemoryScope.AGENT:
            scope_ref = scope_ref or agent.agent_id
            if scope_ref != agent.agent_id:
                raise PermissionDeniedError("an agent may only propose AGENT-scope memory for itself")
        elif scope is MemoryScope.DEPARTMENT:
            scope_ref = scope_ref or agent.department
        elif scope in (MemoryScope.CUSTOMER, MemoryScope.PROJECT) and not scope_ref:
            raise ValidationFailedError(f"{scope.value} memory requires scope_ref")
        if scope is MemoryScope.ORGANIZATION:
            scope_ref = None
        if scope not in agent.memory_scopes:
            raise PermissionDeniedError(f"agent {agent.agent_id!r} has no {scope.value} memory scope")

        now = self.clock()
        expires_at = _parse_dt(candidate.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            raise ValidationFailedError("expires_at must be in the future")
        mem = Memory(memory_id=new_id("mem"), organization_id=organization_id, fact=fact, scope=scope,
                     scope_ref=scope_ref, source_ids=tuple(source_ids),
                     source_refs=tuple(sources.get(s).reference for s in source_ids),
                     confidence=Confidence(candidate.get("confidence", Confidence.MEDIUM.value)),
                     reason_to_remember=reason, proposed_by_agent=agent.agent_id, proposed_in_run=run_id,
                     proposed_at=now, category=candidate.get("category", "OTHER"),
                     review_at=_parse_dt(candidate.get("review_at")), expires_at=expires_at)
        self.repo.add(mem)
        self.audit.record(EventType.MEMORY_PROPOSED, organization_id=organization_id, actor_type=ActorType.AGENT,
                          actor_id=agent.agent_id, agent_id=agent.agent_id, run_id=run_id, object_type="memory",
                          object_id=mem.memory_id, payload={"fact": fact, "scope": scope.value,
                                                            "scope_ref": scope_ref, "source_ids": source_ids},
                          new_state=mem.status)
        return mem

    def _decide(self, memory_id: str, target: MemoryStatus, principal: Principal, reason: str | None,
                event: EventType) -> Memory:
        if not principal.is_human:
            raise PermissionDeniedError("only a human can decide on memory")
        mem = self.repo.get(memory_id)
        if principal.organization_id != mem.organization_id:
            raise PermissionDeniedError("different organization")
        MEMORY_MACHINE.check(mem.status, target)
        updated = self.repo.update(replace(mem, status=target, decided_by=principal.principal_id,
                                           decided_at=self.clock(), decision_reason=reason),
                                   expected_version=mem.version)
        self.audit.record(event, organization_id=mem.organization_id, actor_type=ActorType.HUMAN,
                          actor_id=principal.principal_id, agent_id=mem.proposed_by_agent, run_id=mem.proposed_in_run,
                          object_type="memory", object_id=memory_id, payload={"reason": reason},
                          previous_state=mem.status, new_state=target)
        return updated

    def accept(self, memory_id: str, reviewer: Principal) -> Memory:
        mem = self.repo.get(memory_id)
        if mem.expires_at is not None and mem.expires_at <= self.clock():
            raise ValidationFailedError("cannot accept an already-expired memory")
        return self._decide(memory_id, MemoryStatus.ACCEPTED, reviewer, None, EventType.MEMORY_ACCEPTED)

    def reject(self, memory_id: str, reviewer: Principal, reason: str) -> Memory:
        return self._decide(memory_id, MemoryStatus.REJECTED, reviewer, reason, EventType.MEMORY_REJECTED)

    def invalidate(self, memory_id: str, reviewer: Principal, reason: str) -> Memory:
        if not reason.strip():
            raise ValidationFailedError("invalidation requires a reason")
        return self._decide(memory_id, MemoryStatus.INVALIDATED, reviewer, reason, EventType.MEMORY_INVALIDATED)

    def expire_due(self) -> list[Memory]:
        now = self.clock()
        out = []
        for mem in self.repo.list(lambda m: m.expires_at is not None and m.expires_at <= now
                                  and m.status in (MemoryStatus.PROPOSED, MemoryStatus.ACCEPTED)):
            updated = self.repo.update(replace(mem, status=MemoryStatus.EXPIRED), expected_version=mem.version)
            self.audit.record(EventType.MEMORY_EXPIRED, organization_id=mem.organization_id,
                              actor_type=ActorType.SYSTEM, actor_id="system", agent_id=mem.proposed_by_agent,
                              object_type="memory", object_id=mem.memory_id, previous_state=mem.status,
                              new_state=MemoryStatus.EXPIRED)
            out.append(updated)
        return out

    def review_due(self, memories: Iterable[Memory]) -> list[Memory]:
        """Accepted memories past review_at are flagged for human review (never deleted)."""
        now = self.clock()
        due = [m for m in memories if m.review_at is not None and m.review_at <= now]
        for m in due:
            self.audit.record(EventType.MEMORY_REVIEW_DUE, organization_id=m.organization_id,
                              actor_type=ActorType.SYSTEM, actor_id="system", agent_id=m.proposed_by_agent,
                              object_type="memory", object_id=m.memory_id,
                              payload={"review_at": m.review_at.isoformat()})
        return due

    def context_for(self, agent: AgentProfile, organization_id: str, *, customer_ids: Iterable[str] = (),
                    project_ids: Iterable[str] = ()) -> list[Memory]:
        """Durable context for a run: ACCEPTED, unexpired, in the agent's scopes only."""
        now = self.clock()
        customers, projects = set(customer_ids), set(project_ids)
        scopes = set(agent.memory_scopes)

        def visible(m: Memory) -> bool:
            if m.organization_id != organization_id or m.status is not MemoryStatus.ACCEPTED:
                return False
            if m.expires_at is not None and m.expires_at <= now:
                return False
            if m.scope not in scopes:
                return False
            return {
                MemoryScope.ORGANIZATION: True,
                MemoryScope.DEPARTMENT: m.scope_ref == agent.department,
                MemoryScope.AGENT: m.scope_ref == agent.agent_id,
                MemoryScope.CUSTOMER: m.scope_ref in customers,
                MemoryScope.PROJECT: m.scope_ref in projects,
            }[m.scope]

        return self.repo.list(visible)
