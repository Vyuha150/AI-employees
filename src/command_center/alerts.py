"""Alerts (Part 41) and notifications.

Alerts are de-duplicated by `dedupe_key`: while an alert with the same key is OPEN or
ACKNOWLEDGED, creating it again returns the existing one. Only humans acknowledge,
resolve or dismiss. Notifications are queued in an outbox; nothing is sent externally
unless a sender adapter is configured (none ships in this phase).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Protocol

from .audit import AuditLogger, EventType
from .common import ActorType, Clock, Principal, StrEnum, new_id, utc_now
from .errors import PermissionDeniedError, ValidationFailedError
from .state_machines import ALERT_MACHINE, AlertStatus
from .storage import InMemoryRepository, Repository


class Severity(StrEnum):
    INFO = "INFO"
    WATCH = "WATCH"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Alert:
    alert_id: str
    organization_id: str
    severity: Severity
    category: str
    title: str
    description: str
    source_agent: str | None
    source_ids: tuple[str, ...]
    related_object_type: str | None
    related_object_id: str | None
    created_at: datetime
    status: AlertStatus = AlertStatus.OPEN
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    dedupe_key: str | None = None
    version: int = 0


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Notification:
    notification_id: str
    organization_id: str
    recipient: str            # agent id or human:<ROLE> / human:<principal_id>
    channel: str
    subject: str
    body: str
    alert_id: str | None
    created_at: datetime
    status: NotificationStatus = NotificationStatus.PENDING
    version: int = 0


class NotificationSender(Protocol):
    def send(self, notification: Notification) -> bool: ...


@dataclass
class AlertService:
    audit: AuditLogger
    repo: Repository[Alert] = field(default_factory=lambda: InMemoryRepository("alert_id"))
    notifications: Repository[Notification] = field(default_factory=lambda: InMemoryRepository("notification_id"))
    sender: NotificationSender | None = None
    clock: Clock = utc_now

    def create(self, *, organization_id: str, severity: Severity | str, category: str, title: str, description: str,
               source_agent: str | None = None, source_ids: Iterable[str] = (), related_object_type: str | None = None,
               related_object_id: str | None = None, dedupe_key: str | None = None,
               actor_type: ActorType = ActorType.SYSTEM, actor_id: str = "system", run_id: str | None = None) -> Alert:
        if not title.strip():
            raise ValidationFailedError("alert needs a title")
        if dedupe_key:
            live = self.repo.list(lambda a: a.organization_id == organization_id and a.dedupe_key == dedupe_key
                                  and a.status in (AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED))
            if live:
                return live[0]
        alert = Alert(new_id("alr"), organization_id, Severity(severity), category, title, description, source_agent,
                      tuple(source_ids), related_object_type, related_object_id, self.clock(), dedupe_key=dedupe_key)
        self.repo.add(alert)
        self.audit.record(EventType.ALERT_CREATED, organization_id=organization_id, actor_type=actor_type,
                          actor_id=actor_id, agent_id=source_agent, run_id=run_id, object_type="alert",
                          object_id=alert.alert_id, payload={"severity": alert.severity.value, "category": category,
                                                             "title": title}, new_state=alert.status)
        return alert

    def _human_transition(self, alert_id: str, target: AlertStatus, principal: Principal, event: EventType,
                          note: str | None = None) -> Alert:
        if not principal.is_human:
            raise PermissionDeniedError("only a human can change an alert's state")
        a = self.repo.get(alert_id)
        if a.organization_id != principal.organization_id:
            raise PermissionDeniedError("different organization")
        ALERT_MACHINE.check(a.status, target)
        now = self.clock()
        changes: dict[str, Any] = {"status": target}
        if target is AlertStatus.ACKNOWLEDGED:
            changes.update(acknowledged_by=principal.principal_id, acknowledged_at=now)
        else:
            changes.update(resolved_at=now, resolved_by=principal.principal_id, resolution_note=note)
        updated = self.repo.update(replace(a, **changes), expected_version=a.version)
        self.audit.record(event, organization_id=a.organization_id, actor_type=ActorType.HUMAN,
                          actor_id=principal.principal_id, agent_id=a.source_agent, object_type="alert",
                          object_id=alert_id, payload={"note": note} if note else None, previous_state=a.status,
                          new_state=target)
        return updated

    def acknowledge(self, alert_id: str, principal: Principal) -> Alert:
        return self._human_transition(alert_id, AlertStatus.ACKNOWLEDGED, principal, EventType.ALERT_ACKNOWLEDGED)

    def resolve(self, alert_id: str, principal: Principal, note: str) -> Alert:
        if not note.strip():
            raise ValidationFailedError("resolving an alert needs a note")
        return self._human_transition(alert_id, AlertStatus.RESOLVED, principal, EventType.ALERT_RESOLVED, note)

    def dismiss(self, alert_id: str, principal: Principal, reason: str) -> Alert:
        if not reason.strip():
            raise ValidationFailedError("dismissing an alert needs a reason")
        return self._human_transition(alert_id, AlertStatus.DISMISSED, principal, EventType.ALERT_DISMISSED, reason)

    def list(self, organization_id: str, *, status: Iterable[AlertStatus] | None = None) -> list[Alert]:
        wanted = set(status) if status else None
        return sorted(self.repo.list(lambda a: a.organization_id == organization_id
                                     and (wanted is None or a.status in wanted)), key=lambda a: a.created_at)

    # -- notifications ------------------------------------------------------------------
    def notify(self, *, organization_id: str, recipient: str, subject: str, body: str,
               alert_id: str | None = None, channel: str = "IN_APP") -> Notification:
        n = Notification(new_id("ntf"), organization_id, recipient, channel, subject, body, alert_id, self.clock())
        self.notifications.add(n)
        self.audit.record(EventType.NOTIFICATION_CREATED, organization_id=organization_id,
                          actor_type=ActorType.SYSTEM, actor_id="system", object_type="notification",
                          object_id=n.notification_id, payload={"recipient": recipient, "subject": subject,
                                                                "alert_id": alert_id, "channel": channel})
        if self.sender is not None:
            ok = False
            try:
                ok = bool(self.sender.send(n))
            except Exception:  # delivery failures are recorded, never raised into business flows
                ok = False
            n = self.notifications.update(replace(n, status=NotificationStatus.SENT if ok else NotificationStatus.FAILED),
                                          expected_version=n.version)
        return n
