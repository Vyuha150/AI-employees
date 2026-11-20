"""Realtime event contracts (Part 44).

Audit events are mapped to a small set of frontend events. `payload_summary` is built from an
allow-list of non-sensitive keys; values are truncated, and anything that looks like a secret
is dropped. Payloads, prompts, tool parameters and model text are never forwarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .audit import AuditEvent, EventType
from .common import new_id

SUMMARY_KEYS = ("status", "severity", "title", "category", "risk_class", "tool_name", "action_type", "required_role",
                "reason", "priority", "owner", "validity", "decision", "workflow_id", "step_id", "objective",
                "impact_category", "confidence", "kpi")
_SECRETISH = re.compile(r"(sk-ant-|-----BEGIN|password|secret|token=|apikey|api_key)", re.I)
MAX_VALUE_CHARS = 200

_DIRECT = {
    EventType.AGENT_RUN_CREATED: "run.queued",
    EventType.AGENT_RUN_STARTED: "run.started",
    EventType.MODEL_CALLED: "run.progress",
    EventType.TOOL_REQUESTED: "run.tool_requested",
    EventType.AGENT_RUN_COMPLETED: "run.completed",
    EventType.AGENT_RUN_FAILED: "run.failed",
    EventType.AGENT_RUN_CANCELLED: "run.cancelled",
    EventType.APPROVAL_REQUESTED: "approval.requested",
    EventType.APPROVAL_APPROVED: "approval.approved",
    EventType.APPROVAL_REJECTED: "approval.rejected",
    EventType.TASK_CREATED: "task.created",
    EventType.TASK_UPDATED: "task.updated",
    EventType.OUTPUT_CREATED: "output.created",
    EventType.RECOMMENDATION_CREATED: "recommendation.created",
    EventType.RECOMMENDATION_UPDATED: "recommendation.updated",
    EventType.RECOMMENDATION_APPROVED: "recommendation.updated",
    EventType.RECOMMENDATION_REJECTED: "recommendation.updated",
    EventType.ALERT_CREATED: "alert.created",
    EventType.ALERT_RESOLVED: "alert.resolved",
    EventType.WORKFLOW_STARTED: "workflow.started",
    EventType.WORKFLOW_COMPLETED: "workflow.completed",
    EventType.WORKFLOW_FAILED: "workflow.failed",
}
REALTIME_EVENT_TYPES = frozenset(_DIRECT.values()) | {"run.waiting_approval", "run.resumed"}


@dataclass(frozen=True)
class RealtimeEvent:
    event_id: str
    organization_id: str
    event_type: str
    timestamp: datetime
    object_type: str
    object_id: str
    payload_summary: dict[str, Any]


def _summary(e: AuditEvent) -> dict[str, Any]:
    out: dict[str, Any] = {}
    payload = e.payload
    for k in SUMMARY_KEYS:
        v = payload.get(k)
        if v is None or isinstance(v, (dict, list)):
            continue
        text = str(v)
        if _SECRETISH.search(text):
            continue
        out[k] = text[:MAX_VALUE_CHARS]
    if e.new_state:
        out["state"] = e.new_state
    if e.agent_id:
        out["agent_id"] = e.agent_id
    if e.run_id:
        out["run_id"] = e.run_id
    return out


def to_realtime(e: AuditEvent) -> RealtimeEvent | None:
    etype = _DIRECT.get(e.event_type)
    if e.event_type is EventType.AGENT_RUN_STATE_CHANGED:
        if e.new_state == "APPROVAL_REQUIRED":
            etype = "run.waiting_approval"
        elif e.new_state == "RUNNING" and e.previous_state in ("APPROVAL_REQUIRED", "TOOL_WAIT"):
            etype = "run.resumed"
    if etype is None:
        return None
    return RealtimeEvent(new_id("rt"), e.organization_id, etype, e.timestamp, e.object_type, e.object_id, _summary(e))


class RealtimeBus:
    """In-process fan-out per organization. A websocket/SSE layer subscribes per tenant."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[RealtimeEvent], None]]] = {}

    def subscribe(self, organization_id: str, callback: Callable[[RealtimeEvent], None]) -> Callable[[], None]:
        self._subs.setdefault(organization_id, []).append(callback)
        return lambda: self._subs[organization_id].remove(callback)

    def on_audit_event(self, e: AuditEvent) -> None:
        rt = to_realtime(e)
        if rt is None:
            return
        for cb in list(self._subs.get(rt.organization_id, [])):
            try:
                cb(rt)
            except Exception:
                pass
