"""Command submission (Part 45) with attachment gating (Part 46).

submit(principal, CommandRequest) -> CommandReceipt
  authenticate context (principal from the auth layer; organization must match) ->
  validate agent (registry fails closed) -> check attachments (type, size, scan, classification,
  access) and extract text -> create run -> persist message -> start the runtime through a
  JobExecutor -> return the run id without waiting when the executor is asynchronous.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

from .agents import RunContext
from .attachments import Attachment, DocumentExtractor, check_model_access
from .audit import EventType
from .common import ActorType, Principal, new_id
from .context import ExplicitContext
from .errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from .tenancy import TenantRegistry

MAX_MESSAGE_CHARS = 20000


@dataclass(frozen=True)
class CommandRequest:
    organization_id: str
    requested_agent: str
    message: str
    attachments: tuple[str, ...] = ()
    context_selection: tuple[ExplicitContext, ...] = ()
    conversation_id: str | None = None


@dataclass(frozen=True)
class CommandReceipt:
    run_id: str
    conversation_id: str
    status: str


@dataclass(frozen=True)
class ConversationMessage:
    message_id: str
    organization_id: str
    conversation_id: str
    author: str
    role: str
    text: str
    run_id: str | None
    created_at: datetime


class JobExecutor(Protocol):
    def submit(self, fn: Callable[[], Any]) -> Any: ...


class SyncExecutor:
    """Runs the job inline (CLI and tests)."""

    def submit(self, fn: Callable[[], Any]) -> Any:
        return fn()


class ThreadExecutor:
    """Returns immediately; the job runs on a worker thread. A production deployment would use a
    durable queue instead (the run is already persisted as QUEUED, so a worker can pick it up)."""

    def __init__(self, workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=workers)

    def submit(self, fn: Callable[[], Any]) -> Future:
        return self._pool.submit(fn)


@dataclass
class AttachmentStore:
    items: dict[str, Attachment] = field(default_factory=dict)

    def add(self, att: Attachment) -> Attachment:
        self.items[att.file_id] = att
        return att

    def get(self, organization_id: str, file_id: str) -> Attachment:
        att = self.items.get(file_id)
        if att is None or att.organization_id != organization_id:
            raise NotFoundError("attachment not found")
        return att


@dataclass
class CommandService:
    tenants: TenantRegistry
    attachments: AttachmentStore = field(default_factory=AttachmentStore)
    extractor: DocumentExtractor | None = None
    executor: JobExecutor = field(default_factory=SyncExecutor)
    messages: list[ConversationMessage] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def submit(self, principal: Principal, req: CommandRequest) -> CommandReceipt:
        if req.organization_id != principal.organization_id:
            raise PermissionDeniedError("command organization does not match the authenticated principal")
        cc = self.tenants.for_principal(principal)
        if cc.runtime is None:
            raise ValidationFailedError("no agent runtime is configured for this organization")
        cc.registry.get_agent(req.requested_agent)
        text = req.message.strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise ValidationFailedError(f"message must be 1..{MAX_MESSAGE_CHARS} characters")

        explicit = list(req.context_selection)
        for file_id in req.attachments:
            att = self.attachments.get(principal.organization_id, file_id)
            check = check_model_access(att, principal, cc.policy.attachments)
            if not check.allowed or self.extractor is None:
                reasons = list(check.reasons) or ["no document extractor is configured"]
                cc.audit.record(EventType.ATTACHMENT_REJECTED, organization_id=principal.organization_id,
                                actor_type=ActorType.HUMAN, actor_id=principal.principal_id, object_type="attachment",
                                object_id=file_id, payload={"reasons": reasons})
                raise ValidationFailedError(f"attachment {file_id} cannot be used", reasons)
            explicit.append(ExplicitContext(f"att-{file_id}", att.name, self.extractor.extract(att)))

        conversation_id = req.conversation_id or new_id("conv")
        if req.conversation_id and any(m.conversation_id == conversation_id and
                                       m.organization_id != principal.organization_id for m in self.messages):
            raise NotFoundError("conversation not found")
        run = cc.runs.create_run(req.requested_agent, principal, text[:200])
        with self._lock:
            self.messages.append(ConversationMessage(new_id("msg"), principal.organization_id, conversation_id,
                                                     principal.principal_id, "user", text, run.run_id, cc.clock()))
        cc.audit.record(EventType.COMMAND_RECEIVED, organization_id=principal.organization_id,
                        actor_type=ActorType.HUMAN, actor_id=principal.principal_id, agent_id=req.requested_agent,
                        run_id=run.run_id, object_type="conversation", object_id=conversation_id,
                        payload={"message_chars": len(text), "attachments": list(req.attachments),
                                 "context_items": len(explicit)})
        ctx = RunContext("Command from an organization member", (), (), text, principal,
                         conversation_id=conversation_id, request_id=new_id("req"),
                         requested_agent_id=req.requested_agent, request_timestamp=cc.clock())
        self.executor.submit(lambda: cc.runtime.execute(run.run_id, ctx, message=text, explicit=explicit))
        return CommandReceipt(run.run_id, conversation_id, cc.runs.get(run.run_id).status.value)
