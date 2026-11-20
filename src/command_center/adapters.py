"""External tool adapter contracts (Part 7 of the Phase 4 brief).

Adapters exist for: messaging (send_customer_message), meeting scheduling (schedule_meeting),
CRM updates (modify_customer_record), purchase orders (create_purchase_order) and marketing
publication (publish_marketing_content). Payments, contract signing and the other high-risk
tools have NO adapter and are always refused here.

All external writes are DISABLED by default (organization_policy.external_adapters). An
adapter is used only when its tool is enabled there AND an adapter instance is registered.

Outcome mapping (AdapterExecutor):
  validation errors                      -> ERROR, nothing sent
  exception classified NOT_SENT          -> ERROR (provably never left the process)
  any other exception (timeout, reset)   -> OUTCOME_UNKNOWN
  malformed adapter response             -> OUTCOME_UNKNOWN
  CONFIRMED without provider reference   -> OUTCOME_UNKNOWN
  PENDING_CONFIRMATION / UNKNOWN         -> OUTCOME_UNKNOWN (confirmation/webhook missing, indeterminate)
  REJECTED (provider definitively did not act) -> ERROR
  CONFIRMED with provider reference      -> OK (result_ref = provider reference)
OUTCOME_UNKNOWN is never retried; the gateway marks the approval and a human reconciles.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol

from .common import StrEnum, new_id
from .executors import ToolInvocation, ToolResult, ToolStatus
from .tools import DEFAULT_TOOL_REGISTRY, ToolCategory, ToolRegistry

ADAPTER_TOOLS = ("send_customer_message", "schedule_meeting", "modify_customer_record", "create_purchase_order",
                 "publish_marketing_content")


class AdapterOutcome(StrEnum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"                        # provider definitively did not perform the action
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    UNKNOWN = "UNKNOWN"


class FailureClass(StrEnum):
    NOT_SENT = "NOT_SENT"                        # provably never dispatched (e.g. connection refused)
    AMBIGUOUS = "AMBIGUOUS"                      # may or may not have been performed


@dataclass(frozen=True)
class AdapterRequest:
    organization_id: str
    tool_name: str
    execution_id: str                            # idempotency key passed to the provider
    payload: Mapping[str, Any]
    approval_id: str | None = None


@dataclass(frozen=True)
class AdapterResult:
    outcome: AdapterOutcome
    provider_reference: str | None = None
    detail: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    mock: bool = False
    checked_at: datetime | None = None


class ExternalAdapter(Protocol):
    name: str
    tool_name: str
    supports_status_query: bool

    def validate_request(self, payload: Mapping[str, Any]) -> list[str]: ...
    def execute(self, request: AdapterRequest) -> AdapterResult: ...
    def query_execution_status(self, execution_id: str, provider_reference: str | None) -> AdapterResult | None: ...
    def classify_exception(self, exc: BaseException) -> FailureClass: ...


def default_classify(exc: BaseException) -> FailureClass:
    """Conservative: only failures that prove the request never left are NOT_SENT."""
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror)):
        return FailureClass.NOT_SENT
    return FailureClass.AMBIGUOUS


def _require(payload: Mapping[str, Any], *keys: str) -> list[str]:
    return [f"{k} is required" for k in keys if payload.get(k) in (None, "", [], {})]


VALIDATORS = {
    "send_customer_message": lambda p: _require(p, "recipients", "body")
    + ([] if isinstance(p.get("recipients"), list) else ["recipients must be a list"]),
    "schedule_meeting": lambda p: _require(p, "participants", "start", "duration_minutes"),
    "modify_customer_record": lambda p: _require(p, "customer_id", "changes")
    + ([] if isinstance(p.get("changes"), Mapping) else ["changes must be an object"]),
    "create_purchase_order": lambda p: _require(p, "vendor_id", "amount", "currency"),
    "publish_marketing_content": lambda p: _require(p, "channel", "content"),
}


@dataclass
class MockAdapter:
    """Simulated provider for one tool. Always reports mock=True. Idempotent on execution_id.
    `script` lets tests force outcomes: AdapterResult instances, exceptions, or raw objects."""

    tool_name: str
    script: list[Any] = field(default_factory=list)
    supports_status_query: bool = True
    calls: list[AdapterRequest] = field(default_factory=list)
    _done: dict[str, AdapterResult] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def name(self) -> str:
        return f"mock:{self.tool_name}"

    def validate_request(self, payload: Mapping[str, Any]) -> list[str]:
        return VALIDATORS[self.tool_name](payload)

    def execute(self, request: AdapterRequest) -> AdapterResult:
        with self._lock:
            if request.execution_id in self._done:        # idempotent replay: no second effect
                return self._done[request.execution_id]
            self.calls.append(request)
            item = self.script.pop(0) if self.script else None
            if isinstance(item, BaseException):
                raise item
            if item is not None and not isinstance(item, AdapterResult):
                return item                                  # deliberately malformed, for tests
            result = item or AdapterResult(AdapterOutcome.CONFIRMED, f"mock-{self.tool_name}-{new_id()}",
                                           "simulated", {"mock": True}, mock=True)
            if not result.mock:
                result = AdapterResult(result.outcome, result.provider_reference, result.detail,
                                       {**result.raw, "mock": True}, mock=True)
            self._done[request.execution_id] = result
            return result

    def query_execution_status(self, execution_id: str, provider_reference: str | None) -> AdapterResult | None:
        return self._done.get(execution_id)

    def classify_exception(self, exc: BaseException) -> FailureClass:
        return default_classify(exc)


@dataclass(frozen=True)
class AdapterSetting:
    tool_name: str
    enabled: bool
    adapter: str | None          # adapter name expected (e.g. "mock:send_customer_message", "twilio")


@dataclass
class AdapterExecutor:
    """ToolExecutor for external writes via registered adapters, gated by organization policy."""

    settings: Mapping[str, AdapterSetting]
    adapters: Mapping[str, ExternalAdapter] = field(default_factory=dict)
    tools: ToolRegistry = DEFAULT_TOOL_REGISTRY

    def enabled_tools(self) -> list[str]:
        return sorted(t for t, s in self.settings.items() if s.enabled and t in self.adapters)

    def execute(self, inv: ToolInvocation) -> ToolResult:
        spec = self.tools.get(inv.tool_name)
        if spec.category is ToolCategory.HIGH_RISK:
            return ToolResult(False, error=f"{inv.tool_name} has no external adapter; high-risk execution is not "
                                           "activated", status=ToolStatus.UNAVAILABLE, tool_name=inv.tool_name)
        setting = self.settings.get(inv.tool_name)
        adapter = self.adapters.get(inv.tool_name)
        if setting is None or not setting.enabled or adapter is None:
            return ToolResult(False, error=f"external writes are disabled for {inv.tool_name}; nothing was sent",
                              status=ToolStatus.UNAVAILABLE, tool_name=inv.tool_name)
        if setting.adapter and setting.adapter != adapter.name:
            return ToolResult(False, error=f"configured adapter {setting.adapter!r} is not the registered adapter",
                              status=ToolStatus.UNAVAILABLE, tool_name=inv.tool_name)
        errors = adapter.validate_request(inv.payload)
        if errors:
            return ToolResult(False, error="invalid request (not sent): " + "; ".join(errors), status=ToolStatus.ERROR,
                              tool_name=inv.tool_name)
        if not inv.execution_id:
            return ToolResult(False, error="no execution id; refusing a non-idempotent external call",
                              status=ToolStatus.ERROR, tool_name=inv.tool_name)
        request = AdapterRequest(inv.organization_id, inv.tool_name, inv.execution_id, dict(inv.payload),
                                 inv.approval_id)
        try:
            result = adapter.execute(request)
        except Exception as exc:
            if adapter.classify_exception(exc) is FailureClass.NOT_SENT:
                return ToolResult(False, error=f"not sent: {type(exc).__name__}: {exc}", status=ToolStatus.ERROR,
                                  tool_name=inv.tool_name)
            return ToolResult(False, error=f"outcome unknown: {type(exc).__name__}: {exc}",
                              status=ToolStatus.EXECUTION_UNKNOWN, tool_name=inv.tool_name)
        if not isinstance(result, AdapterResult):
            return ToolResult(False, error=f"malformed provider response ({type(result).__name__})",
                              status=ToolStatus.EXECUTION_UNKNOWN, tool_name=inv.tool_name)
        data = {"provider_reference": result.provider_reference, "outcome": result.outcome.value,
                "adapter": adapter.name, "mock": result.mock}
        if result.outcome is AdapterOutcome.CONFIRMED and result.provider_reference:
            return ToolResult(True, result.provider_reference, data, status=ToolStatus.OK, tool_name=inv.tool_name,
                              mock=result.mock)
        if result.outcome is AdapterOutcome.REJECTED:
            return ToolResult(False, error=f"provider rejected the request: {result.detail}", status=ToolStatus.ERROR,
                              data=data, tool_name=inv.tool_name, mock=result.mock)
        why = {AdapterOutcome.CONFIRMED: "confirmed without a provider reference",
               AdapterOutcome.PENDING_CONFIRMATION: "accepted but confirmation not received",
               AdapterOutcome.UNKNOWN: "provider reported an indeterminate state"}[result.outcome]
        return ToolResult(False, error=f"outcome unknown: {why}", status=ToolStatus.EXECUTION_UNKNOWN, data=data,
                          tool_name=inv.tool_name, mock=result.mock)

    def provider_status(self, tool_name: str, execution_id: str, provider_reference: str | None) -> AdapterResult | None:
        """Evidence for a human reconciler. Never changes any state by itself."""
        adapter = self.adapters.get(tool_name)
        if adapter is None or not adapter.supports_status_query:
            return None
        return adapter.query_execution_status(execution_id, provider_reference)


def adapter_settings_from_policy(raw: Mapping[str, Any] | None) -> dict[str, AdapterSetting]:
    out = {t: AdapterSetting(t, False, None) for t in ADAPTER_TOOLS}       # disabled by default
    for tool, cfg in (raw or {}).items():
        if tool not in ADAPTER_TOOLS:
            raise ValueError(f"external_adapters: {tool!r} has no adapter contract")
        out[tool] = AdapterSetting(tool, bool(cfg.get("enabled", False)), cfg.get("adapter"))
    return out
