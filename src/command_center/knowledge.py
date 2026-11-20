"""Knowledge ingestion and retrieval (Part 47).

ingest(KnowledgeInput) -> IngestionResult(document, chunks, sources)

Retrieval is permission- and scope-filtered: a chunk is returned only when its document is
valid now, the requesting principal holds one of the document's roles (or the document is
open to "ALL" members), and the document's category is one of the agent's knowledge_scopes.
`KeywordRetriever` is the current implementation; `Retriever` is the contract an embedding
retriever implements later (chunks already carry an optional `embedding` slot). No vector
database is used in this phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable, Protocol, Sequence

from .audit import AuditLogger, EventType
from .common import ActorType, Clock, Principal, new_id, utc_now
from .errors import PermissionDeniedError, ValidationFailedError
from .sources import AuthorityLevel, SourceRecord, SourceType
from .storage import InMemoryRepository, Repository

_WORD = re.compile(r"[a-z0-9]+")
MAX_CHUNK_CHARS = 1500


@dataclass(frozen=True)
class KnowledgeInput:
    title: str
    text: str
    category: str                    # must match an agent knowledge scope to be retrievable
    owner: str
    permissions: tuple[str, ...]     # roles allowed to read, or ("ALL",)
    source_reference: str            # where the document came from (URL, file id, system record)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    authority_level: AuthorityLevel = AuthorityLevel.SECONDARY


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    organization_id: str
    title: str
    category: str
    owner: str
    permissions: tuple[str, ...]
    source_reference: str
    valid_from: datetime | None
    valid_until: datetime | None
    authority_level: AuthorityLevel
    created_at: datetime
    created_by: str
    version: int = 0


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    organization_id: str
    document_id: str
    ordinal: int
    text: str
    embedding: tuple[float, ...] | None = None
    version: int = 0


@dataclass(frozen=True)
class IngestionResult:
    document: KnowledgeDocument
    chunks: tuple[KnowledgeChunk, ...]
    sources: tuple[SourceRecord, ...]


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: KnowledgeChunk
    document: KnowledgeDocument
    score: float
    source: SourceRecord


class Retriever(Protocol):
    def retrieve(self, *, organization_id: str, query: str, scopes: Iterable[str], principal: Principal,
                 limit: int, now: datetime) -> list[RetrievedChunk]: ...


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:max_chars])
            p = p[max_chars:]
        if len(cur) + len(p) + 2 > max_chars and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def chunk_source(doc: KnowledgeDocument, chunk: KnowledgeChunk, retrieved_at: datetime) -> SourceRecord:
    return SourceRecord(f"kb:{chunk.chunk_id}", SourceType.DOCUMENT, f"{doc.title} (part {chunk.ordinal + 1})",
                        f"knowledge:{doc.document_id}#{chunk.ordinal} <- {doc.source_reference}",
                        retrieved_at.isoformat(), doc.authority_level)


@dataclass
class KnowledgeService:
    organization_id: str
    audit: AuditLogger
    documents: Repository[KnowledgeDocument] = field(default_factory=lambda: InMemoryRepository("document_id"))
    chunks: Repository[KnowledgeChunk] = field(default_factory=lambda: InMemoryRepository("chunk_id"))
    clock: Clock = utc_now

    def ingest(self, item: KnowledgeInput, principal: Principal) -> IngestionResult:
        if not principal.is_human or principal.organization_id != self.organization_id:
            raise PermissionDeniedError("knowledge is ingested by a human of the same organization")
        if not item.text.strip() or not item.title.strip() or not item.source_reference.strip():
            raise ValidationFailedError("knowledge needs a title, text and a source reference")
        if not item.permissions:
            raise ValidationFailedError("knowledge needs explicit read permissions (roles or ALL)")
        if item.valid_until and item.valid_from and item.valid_until <= item.valid_from:
            raise ValidationFailedError("valid_until must be after valid_from")
        now = self.clock()
        doc = KnowledgeDocument(new_id("doc"), self.organization_id, item.title, item.category, item.owner,
                                tuple(item.permissions), item.source_reference, item.valid_from, item.valid_until,
                                item.authority_level, now, principal.principal_id)
        self.documents.add(doc)
        chunks = tuple(KnowledgeChunk(new_id("chk"), self.organization_id, doc.document_id, i, t)
                       for i, t in enumerate(chunk_text(item.text)))
        for c in chunks:
            self.chunks.add(c)
        self.audit.record(EventType.KNOWLEDGE_INGESTED, organization_id=self.organization_id,
                          actor_type=ActorType.HUMAN, actor_id=principal.principal_id, object_type="knowledge_document",
                          object_id=doc.document_id, payload={"title": doc.title, "category": doc.category,
                                                              "chunks": len(chunks)})
        return IngestionResult(doc, chunks, tuple(chunk_source(doc, c, now) for c in chunks))

    def readable(self, doc: KnowledgeDocument, principal: Principal, now: datetime) -> bool:
        if doc.organization_id != principal.organization_id:
            return False
        if doc.valid_from and now < doc.valid_from:
            return False
        if doc.valid_until and now >= doc.valid_until:
            return False
        return "ALL" in doc.permissions or bool(set(doc.permissions) & set(principal.roles))


@dataclass
class KeywordRetriever:
    knowledge: KnowledgeService

    def retrieve(self, *, organization_id: str, query: str, scopes: Iterable[str], principal: Principal,
                 limit: int, now: datetime) -> list[RetrievedChunk]:
        scopes = set(scopes)
        terms = set(_WORD.findall(query.lower()))
        docs = {d.document_id: d for d in self.knowledge.documents.list(
            lambda d: d.organization_id == organization_id and d.category in scopes)}
        docs = {k: d for k, d in docs.items() if self.knowledge.readable(d, principal, now)}
        scored = []
        for c in self.knowledge.chunks.list(lambda c: c.document_id in docs):
            words = _WORD.findall(c.text.lower())
            if not words:
                continue
            hits = sum(1 for w in words if w in terms)
            if hits:
                scored.append((hits / len(words) ** 0.5, c))
        scored.sort(key=lambda x: (-x[0], x[1].chunk_id))
        return [RetrievedChunk(c, docs[c.document_id], s, chunk_source(docs[c.document_id], c, now))
                for s, c in scored[:limit]]


def with_embedding(chunk: KnowledgeChunk, vector: Sequence[float]) -> KnowledgeChunk:
    """Future embedding retrievers store vectors here; nothing computes them in this phase."""
    return replace(chunk, embedding=tuple(float(v) for v in vector))
