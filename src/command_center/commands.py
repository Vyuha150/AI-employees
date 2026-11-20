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
from .state_machines import RunStatus
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
    idempotency_key: str | None = None      # client-supplied; duplicates return the original receipt


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
    """Commands are persisted to the tenant's ConversationService. With a durable `queue`
    (production) the run is enqueued as an `agent_run` job and executed by a JobWorker; without
    one, `executor` runs it (SyncExecutor / ThreadExecutor for development and tests)."""

    tenants: TenantRegistry
    attachments: AttachmentStore = field(default_factory=AttachmentStore)
    extractor: DocumentExtractor | None = None
    executor: JobExecutor = field(default_factory=SyncExecutor)
    queue: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def messages(self) -> list[Any]:
        """All persisted messages of every tenant this service serves (diagnostics/tests)."""
        out: list[Any] = []
        for cc in self.tenants.tenants.values():
            if cc.conversations is not None:
                out.extend(cc.conversations.all_messages())
        return out

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

        if self.queue is not None and req.idempotency_key:
            existing = self.queue.find_by_key(principal.organization_id, f"command:{req.idempotency_key}")
            if existing is not None:
                return CommandReceipt(existing.run_id, existing.payload.get("conversation_id", ""), "DUPLICATE")

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

        convs = cc.conversations
        if req.conversation_id:
            conv = convs.get(principal, req.conversation_id)          # NotFound across organizations
        else:
            conv = convs.create(principal, title=text[:80], primary_agent_id=req.requested_agent)
        run = cc.runs.create_run(req.requested_agent, principal, text[:200])
        convs.add_message(conv.conversation_id, sender_type=ActorType.HUMAN, sender_id=principal.principal_id,
                          content=text, run_id=run.run_id, attachment_ids=req.attachments)
        request_id = new_id("req")
        cc.audit.record(EventType.COMMAND_RECEIVED, organization_id=principal.organization_id,
                        actor_type=ActorType.HUMAN, actor_id=principal.principal_id, agent_id=req.requested_agent,
                        run_id=run.run_id, object_type="conversation", object_id=conv.conversation_id,
                        payload={"message_chars": len(text), "attachments": list(req.attachments),
                                 "context_items": len(explicit)})

        if self.queue is not None:
            from .jobs import AGENT_RUN, SideEffect, new_job
            job, created = self.queue.enqueue(new_job(
                organization_id=principal.organization_id, job_type=AGENT_RUN,
                idempotency_key=f"command:{req.idempotency_key or run.run_id}", side_effect=SideEffect.INTERNAL,
                run_id=run.run_id, now=cc.clock(), timeout_seconds=900,
                max_attempts=2,     # one reclaim if the worker died before the run started (never a retry)
                payload={"message": text, "conversation_id": conv.conversation_id, "request_id": request_id,
                         "explicit": [{"item_id": e.item_id, "title": e.title, "text": e.text} for e in explicit]}))
            if not created:   # lost a race with an identical submission: never run twice
                cc.runs.transition(run.run_id, RunStatus.CANCELLED, reason="duplicate idempotency key")
                return CommandReceipt(job.run_id, job.payload.get("conversation_id", ""), "DUPLICATE")
            cc.audit.record(EventType.JOB_ENQUEUED, organization_id=principal.organization_id,
                            actor_type=ActorType.SYSTEM, actor_id="commands", run_id=run.run_id, object_type="job",
                            object_id=job.job_id, payload={"job_type": job.job_type})
            return CommandReceipt(run.run_id, conv.conversation_id, cc.runs.get(run.run_id).status.value)

        ctx = RunContext("Command from an organization member", (), (), text, principal,
                         conversation_id=conv.conversation_id, request_id=request_id,
                         requested_agent_id=req.requested_agent, request_timestamp=cc.clock())

        def job() -> Any:
            result = cc.runtime.execute(run.run_id, ctx, message=text, explicit=explicit)
            if result.output_id:
                out = cc.outputs.get(result.output_id)
                if out.validity.value == "VALID":
                    convs.add_message(conv.conversation_id, sender_type=ActorType.AGENT, sender_id=run.agent_id,
                                      agent_id=run.agent_id, content=out.content.get("executive_summary", ""),
                                      structured_content={"output_id": out.output_id}, run_id=run.run_id)
            return result
        self.executor.submit(job)
        return CommandReceipt(run.run_id, conv.conversation_id, cc.runs.get(run.run_id).status.value)
