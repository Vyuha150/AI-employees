"""Context assembler (Part 32) and source provenance (Part 33).

Assembles only what the target agent may see, within configured limits:
  explicit user context -> organization policy facts -> KPI snapshots (if the agent holds
  read_kpis; only its own KPIs) -> accepted memories in its scopes -> knowledge in its
  knowledge_scopes readable by the principal -> its own recent valid outputs.
Lower-priority items are dropped first when limits are hit; drops are reported.

Every item becomes a SourceRecord in a SourceLedger. The ledger is the closed set of sources
the model may cite: the runtime rejects responses citing sources that were not provided.
No anonymous business facts are passed in structured mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .agents import AgentProfile, RunContext
from .audit import AuditLogger, EventType
from .common import ActorType, Clock, utc_now
from .policy import ContextLimits, OrganizationPolicy
from .sources import AuthorityLevel, SourceRecord, SourceType


class SourceLedger:
    def __init__(self) -> None:
        self._sources: dict[str, SourceRecord] = {}

    def add(self, source: SourceRecord) -> SourceRecord:
        existing = self._sources.get(source.source_id)
        if existing is not None and existing.reference != source.reference:
            raise ValueError(f"source id {source.source_id!r} reused for a different reference")
        self._sources.setdefault(source.source_id, source)
        return self._sources[source.source_id]

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._sources

    def __len__(self) -> int:
        return len(self._sources)

    def all(self) -> list[SourceRecord]:
        return list(self._sources.values())

    def to_wire(self) -> list[dict[str, Any]]:
        out = []
        for s in self._sources.values():
            item = {"id": s.source_id, "type": s.source_type.value, "reference": s.reference}
            if s.title:
                item["title"] = s.title
            if s.retrieved_at:
                item["retrieved_at"] = s.retrieved_at
            if s.authority_level:
                item["authority_level"] = s.authority_level.value
            out.append(item)
        return out

    def provenance_errors(self, response: Mapping[str, Any]) -> list[str]:
        """Sources in a model response must be exactly sources the runtime provided."""
        errors = []
        for s in response.get("sources", []) if isinstance(response, Mapping) else []:
            sid = s.get("id")
            known = self._sources.get(sid)
            if known is None:
                errors.append(f"provenance: source {sid!r} was not provided to the model")
            elif s.get("reference") != known.reference:
                errors.append(f"provenance: source {sid!r} reference does not match the provided record")
        return errors


@dataclass(frozen=True)
class ContextItem:
    kind: str
    text: str
    source: SourceRecord
    priority: int            # lower = kept first


@dataclass(frozen=True)
class ExplicitContext:
    """User-provided context selected for this command (already access-checked by the caller)."""

    item_id: str
    title: str
    text: str


@dataclass
class AssembledContext:
    items: list[ContextItem]
    ledger: SourceLedger
    dropped: list[str]
    stale_memory_ids: list[str]

    def lines(self, kind: str) -> tuple[str, ...]:
        return tuple(f"[{i.source.source_id}] {i.text}" for i in self.items if i.kind == kind)

    @property
    def size(self) -> int:
        return sum(len(i.text) for i in self.items)


@dataclass
class ContextAssembler:
    policy: OrganizationPolicy
    audit: AuditLogger
    memory: Any = None          # MemoryService
    retriever: Any = None       # knowledge.Retriever
    kpis: Any = None            # KpiService
    outputs: Any = None         # OutputService
    clock: Clock = utc_now

    @property
    def limits(self) -> ContextLimits:
        return self.policy.context_limits

    def assemble(self, agent: AgentProfile, ctx: RunContext, *, run_id: str | None = None,
                 explicit: Iterable[ExplicitContext] = (), extra_items: Iterable[ContextItem] = (),
                 customer_ids: Iterable[str] = (), project_ids: Iterable[str] = ()) -> AssembledContext:
        now = self.clock()
        stamp = now.isoformat()
        org = ctx.organization_id
        lim = self.limits
        items: list[ContextItem] = []

        for e in explicit:
            items.append(ContextItem("explicit", f"{e.title}: {e.text}",
                                     SourceRecord(f"usr:{e.item_id}", SourceType.USER_MESSAGE, e.title,
                                                  f"user:{ctx.user_id}:{e.item_id}", stamp, AuthorityLevel.UNVERIFIED), 0))
        items.extend(extra_items)
        items.append(ContextItem("policy", f"Organization currency {self.policy.currency}; timezone "
                                           f"{ctx.timezone or self.policy.timezone}.",
                                 SourceRecord(f"cfg:policy:{org}", SourceType.SYSTEM_CONFIG, "Organization policy",
                                              f"config:organization_policy:{org}", stamp,
                                              AuthorityLevel.AUTHORITATIVE), 1))

        if self.kpis is not None and "read_kpis" in agent.allowed_tools:
            n = 0
            for k in agent.kpis:
                snap = self.kpis.latest(org, k.name)
                if snap is None or n >= lim.max_kpi_snapshots:
                    continue
                n += 1
                items.append(ContextItem("kpi", f"{snap.kpi} = {snap.value} {snap.unit} (observed {snap.observed_at})",
                                         SourceRecord(f"kpi:{snap.snapshot_id}", SourceType.COMPANY_DATABASE,
                                                      f"KPI {snap.kpi}", f"kpi_snapshots:{snap.snapshot_id} <- "
                                                      f"{snap.source_reference}", stamp,
                                                      AuthorityLevel.AUTHORITATIVE), 2))

        stale: list[str] = []
        if self.memory is not None:
            mems = self.memory.context_for(agent, org, customer_ids=customer_ids, project_ids=project_ids)
            mems = sorted(mems, key=lambda m: m.decided_at or m.proposed_at, reverse=True)[:lim.max_memory_items]
            due = {m.memory_id for m in self.memory.review_due(mems)}
            stale = sorted(due)
            for m in mems:
                flag = " [REVIEW DUE: treat as possibly outdated]" if m.memory_id in due else ""
                items.append(ContextItem("memory", f"{m.fact}{flag}",
                                         SourceRecord(f"mem:{m.memory_id}", SourceType.ACCEPTED_MEMORY,
                                                      f"Accepted memory ({m.scope.value})",
                                                      f"memory:{m.memory_id} <- {', '.join(m.source_refs)}", stamp,
                                                      AuthorityLevel.SECONDARY), 3))

        if self.retriever is not None and ctx.current_task.strip():
            hits = self.retriever.retrieve(organization_id=org, query=ctx.current_task, scopes=agent.knowledge_scopes,
                                           principal=ctx.current_user, limit=lim.max_knowledge_items, now=now)
            for h in hits:
                items.append(ContextItem("knowledge", h.chunk.text, h.source, 4))

        if self.outputs is not None:
            recent = self.outputs.repo.list(lambda o: o.agent_id == agent.agent_id and o.organization_id == org
                                            and o.validity.value == "VALID" and o.run_id != run_id)
            for o in sorted(recent, key=lambda o: o.created_at, reverse=True)[:lim.max_recent_outputs]:
                items.append(ContextItem("output", f"Earlier output ({o.response_type}): "
                                                   f"{o.content.get('executive_summary', '')}",
                                         SourceRecord(f"out:{o.output_id}", SourceType.AGENT_OUTPUT,
                                                      f"Output {o.output_id}", f"outputs:{o.output_id}",
                                                      stamp, AuthorityLevel.SECONDARY), 5))

        # enforce limits: keep by priority, then insertion order
        items.sort(key=lambda i: i.priority)
        kept, dropped, size = [], [], 0
        for i in items:
            if len(kept) >= lim.max_source_count or size + len(i.text) > lim.max_context_chars:
                dropped.append(f"{i.kind}:{i.source.source_id}")
                continue
            kept.append(i)
            size += len(i.text)
        ledger = SourceLedger()
        for i in kept:
            ledger.add(i.source)
        self.audit.record(EventType.CONTEXT_ASSEMBLED, organization_id=org, actor_type=ActorType.SYSTEM,
                          actor_id="context", agent_id=agent.agent_id, run_id=run_id, object_type="agent_run",
                          object_id=run_id or "-", payload={"items": len(kept), "dropped": len(dropped),
                                                            "chars": size, "stale_memories": len(stale)})
        return AssembledContext(kept, ledger, dropped, stale)
