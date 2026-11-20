"""Conversation persistence (Part 4 of the Phase 4 brief).

- Every conversation and message belongs to one organization; lookups across organizations are
  NotFound.
- Messages are immutable once stored (no update API; the database rejects UPDATE/DELETE).
- Messages are returned in chronological order (per-conversation `seq`) with cursor pagination.
- Attachments are referenced by id only; message content is never a file body.
- Anything that looks like a credential is redacted before storage and the redaction is audited.
- History is NOT fed to the model automatically: the context assembler decides what a run sees.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping

from .audit import AuditLogger, EventType
from .common import ActorType, Clock, Principal, StrEnum, new_id, utc_now
from .errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from .storage import InMemoryRepository, Repository

MAX_MESSAGE_CHARS = 20000
_SECRETS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"postgres(?:ql)?://[^\s:@]+:[^\s@]+@\S+"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|bearer)\s*[:=]\s*\S+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
]
REDACTED = "[REDACTED CREDENTIAL]"


def redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    for pat in _SECRETS:
        text, n = pat.subn(REDACTED, text)
        count += n
    return text, count


def _redact_structured(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        total, out = 0, {}
        for k, v in value.items():
            if re.search(r"(?i)(api[_-]?key|secret|password|token|credential)", str(k)) and not str(k).endswith("_env"):
                out[k], total = REDACTED, total + 1
                continue
            out[k], n = _redact_structured(v)
            total += n
        return out, total
    if isinstance(value, (list, tuple)):
        total, items = 0, []
        for v in value:
            r, n = _redact_structured(v)
            items.append(r)
            total += n
        return items, total
    return value, 0


class ConversationStatus(StrEnum):
    OPEN = "OPEN"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True)
class Participant:
    actor_type: ActorType
    actor_id: str


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    organization_id: str
    title: str
    created_by: str
    primary_agent_id: str | None
    created_at: datetime
    updated_at: datetime
    status: ConversationStatus = ConversationStatus.OPEN
    participants: tuple[Participant, ...] = ()
    message_count: int = 0
    version: int = 0


@dataclass(frozen=True)
class Message:
    message_id: str
    organization_id: str
    conversation_id: str
    seq: int
    sender_type: ActorType
    sender_id: str
    agent_id: str | None
    content: str
    structured_content: Mapping[str, Any] | None
    run_id: str | None
    created_at: datetime
    attachment_ids: tuple[str, ...] = ()
    version: int = 0

    @property
    def text(self) -> str:      # compatibility with the earlier in-memory message shape
        return self.content


@dataclass(frozen=True)
class MessagePage:
    items: tuple[Message, ...]
    next_cursor: int | None      # pass as `after_seq` to get the next page


@dataclass
class ConversationService:
    organization_id: str
    audit: AuditLogger
    conversations: Repository[Conversation] = field(default_factory=lambda: InMemoryRepository("conversation_id"))
    messages: Repository[Message] = field(default_factory=lambda: InMemoryRepository("message_id"))
    clock: Clock = utc_now
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def _member(self, principal: Principal) -> None:
        if principal.organization_id != self.organization_id:
            raise PermissionDeniedError("principal belongs to a different organization")

    def create(self, principal: Principal, *, title: str, primary_agent_id: str | None = None,
               conversation_id: str | None = None) -> Conversation:
        self._member(principal)
        now = self.clock()
        parts = [Participant(principal.actor_type, principal.principal_id)]
        if primary_agent_id:
            parts.append(Participant(ActorType.AGENT, primary_agent_id))
        conv = Conversation(conversation_id or new_id("conv"), self.organization_id, redact_secrets(title[:200])[0],
                            principal.principal_id, primary_agent_id, now, now, participants=tuple(parts))
        self.conversations.add(conv)
        self.audit.record(EventType.CONVERSATION_CREATED, organization_id=self.organization_id,
                          actor_type=principal.actor_type, actor_id=principal.principal_id, agent_id=primary_agent_id,
                          object_type="conversation", object_id=conv.conversation_id)
        return conv

    def get(self, principal: Principal, conversation_id: str) -> Conversation:
        self._member(principal)
        conv = self.conversations.find(conversation_id)
        if conv is None or conv.organization_id != principal.organization_id:
            raise NotFoundError("conversation not found")
        return conv

    def add_message(self, conversation_id: str, *, sender_type: ActorType, sender_id: str, content: str,
                    agent_id: str | None = None, structured_content: Mapping[str, Any] | None = None,
                    run_id: str | None = None, attachment_ids: Iterable[str] = ()) -> Message:
        if not content.strip() and not structured_content:
            raise ValidationFailedError("a message needs content")
        if len(content) > MAX_MESSAGE_CHARS:
            raise ValidationFailedError(f"message exceeds {MAX_MESSAGE_CHARS} characters")
        content, n1 = redact_secrets(content)
        structured, n2 = _redact_structured(structured_content) if structured_content else (None, 0)
        with self._lock:
            conv = self.conversations.find(conversation_id)
            if conv is None or conv.organization_id != self.organization_id:
                raise NotFoundError("conversation not found")
            if conv.status is not ConversationStatus.OPEN:
                raise ValidationFailedError("conversation is archived")
            seq = conv.message_count + 1
            msg = Message(new_id("msg"), self.organization_id, conversation_id, seq, sender_type, sender_id, agent_id,
                          content, structured, run_id, self.clock(), tuple(attachment_ids))
            self.messages.add(msg)
            parts = conv.participants
            if Participant(sender_type, sender_id) not in parts:
                parts = parts + (Participant(sender_type, sender_id),)
            self.conversations.update(replace(conv, message_count=seq, updated_at=msg.created_at, participants=parts),
                                      expected_version=conv.version)
        self.audit.record(EventType.MESSAGE_CREATED, organization_id=self.organization_id, actor_type=sender_type,
                          actor_id=sender_id, agent_id=agent_id, run_id=run_id, object_type="conversation",
                          object_id=conversation_id, payload={"message_id": msg.message_id, "seq": seq,
                                                              "chars": len(content),
                                                              "attachments": list(msg.attachment_ids)})
        if n1 + n2:
            self.audit.record(EventType.MESSAGE_REDACTED, organization_id=self.organization_id,
                              actor_type=ActorType.SYSTEM, actor_id="conversations", object_type="message",
                              object_id=msg.message_id, payload={"redactions": n1 + n2})
        return msg

    def list_messages(self, principal: Principal, conversation_id: str, *, after_seq: int = 0,
                      limit: int = 50) -> MessagePage:
        self.get(principal, conversation_id)
        limit = max(1, min(limit, 200))
        query = getattr(self.messages, "query", None)
        if query is not None:
            rows = query({"conversation_id": conversation_id, "seq__gt": after_seq}, order_by="seq", limit=limit + 1)
        else:
            rows = sorted(self.messages.list(lambda m: m.conversation_id == conversation_id and m.seq > after_seq),
                          key=lambda m: m.seq)[:limit + 1]
        has_more = len(rows) > limit
        rows = rows[:limit]
        return MessagePage(tuple(rows), rows[-1].seq if has_more and rows else None)

    def list_conversations(self, principal: Principal, *, limit: int = 50) -> list[Conversation]:
        self._member(principal)
        query = getattr(self.conversations, "query", None)
        if query is not None:
            return query({}, order_by="-updated_at", limit=limit)
        return sorted(self.conversations.list(lambda c: c.organization_id == self.organization_id),
                      key=lambda c: c.updated_at, reverse=True)[:limit]

    def all_messages(self) -> list[Message]:
        return sorted(self.messages.list(), key=lambda m: (m.created_at, m.seq))
