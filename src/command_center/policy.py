"""Organization policy: thresholds, approver roles, TTLs and orchestration limits.

All numbers come from organization configuration. Nothing here assumes a currency or market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import yaml

from .common import RiskClass, StrEnum
from .errors import ConfigurationError
from .resources import Resource, as_resource, default_policy_file
from .tools import DEFAULT_TOOL_REGISTRY, ToolRegistry

DEFAULT_POLICY_PATH = default_policy_file()  # packaged default (importlib.resources)


def _dec(value: Any, label: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ConfigurationError(f"{label} must be numeric") from None
    if not d.is_finite() or d < 0:
        raise ConfigurationError(f"{label} must be a finite non-negative number")
    return d


@dataclass(frozen=True)
class MonetaryRule:
    amount_field: str
    currency_field: str
    amber_at: Decimal
    red_at: Decimal
    required: bool = True   # missing amount on a tool with a required rule -> RED (fail closed)


@dataclass(frozen=True)
class RecipientRule:
    field: str
    red_at_count: int


@dataclass(frozen=True)
class SensitiveFieldRule:
    field: str
    red_if_keys: frozenset[str]


@dataclass(frozen=True)
class OrchestrationLimits:
    max_agents_per_orchestration: int = 4
    max_delegation_depth: int = 1
    max_total_cost: float = 5.0
    max_total_tokens: int = 500_000
    max_runtime_seconds: int = 900
    cost_currency: str = "USD"


class ExhaustionPolicy(StrEnum):
    WARN = "WARN"
    BLOCK_NEW_RUNS = "BLOCK_NEW_RUNS"
    REQUIRE_OVERRIDE = "REQUIRE_OVERRIDE"


@dataclass(frozen=True)
class BudgetPolicy:
    currency: str
    organization_max_cost_per_day: float | None
    organization_max_cost_per_month: float | None
    warn_at_fraction: float
    exhaustion_policy: ExhaustionPolicy
    override_role: str | None
    override_max_hours: float


# Conservative values used only when a policy mapping omits a section programmatically.
# `config validate` reports every omitted security-relevant section as an error.
CONSERVATIVE_BUDGETS = BudgetPolicy("USD", None, None, 0.8, ExhaustionPolicy.BLOCK_NEW_RUNS, None, 24.0)


@dataclass(frozen=True)
class SchedulerPolicy:
    enabled: bool = False
    max_catch_up_runs: int = 1
    lookback_hours: float = 24.0
    escalation_cron: str | None = None      # periodic escalation evaluation (system job); None = off


BRIEFING_CONDITIONS = frozenset({"PRODUCT_ACTIVITY", "PURCHASE_ACTIVITY", "PEOPLE_AUTHORIZED"})


@dataclass(frozen=True)
class BriefingPolicy:
    """Explicit participants of the system brief workflows. Workflows never add agents dynamically."""

    daily_data_agent: str = "data_analyst"
    daily_specialists: tuple[str, ...] = ("cfo", "sales_director", "operations", "customer_success",
                                          "marketing_director")
    weekly_core: tuple[str, ...] = ("sales_director", "cfo", "operations", "customer_success", "marketing_director",
                                    "legal_risk")
    weekly_conditional: Mapping[str, str] = field(default_factory=lambda: {
        "product_analyst": "PRODUCT_ACTIVITY", "procurement": "PURCHASE_ACTIVITY",
        "people_performance": "PEOPLE_AUTHORIZED"})

    def all_agents(self) -> set[str]:
        return {self.daily_data_agent, *self.daily_specialists, *self.weekly_core, *self.weekly_conditional,
                "chief_of_staff"}


@dataclass(frozen=True)
class ContextLimits:
    max_memory_items: int = 10
    max_knowledge_items: int = 5
    max_recent_outputs: int = 3
    max_kpi_snapshots: int = 10
    max_source_count: int = 30
    max_context_chars: int = 30000


@dataclass(frozen=True)
class RetrySetting:
    max_attempts: int
    backoff_seconds: float


@dataclass(frozen=True)
class RetryPolicies:
    model: RetrySetting = RetrySetting(1, 0.0)
    read_tool: RetrySetting = RetrySetting(1, 0.0)
    write_tool: RetrySetting = RetrySetting(1, 0.0)


ESCALATION_TYPES = frozenset({
    "TASK_OVERDUE", "TASK_BLOCKED_TOO_LONG", "APPROVAL_PENDING_TOO_LONG", "TOOL_FAILURE_REPEATED",
    "RUN_FAILURE_REPEATED", "EXECUTION_UNKNOWN", "KPI_THRESHOLD", "CRITICAL_RISK_DETECTED", "DEAL_STALLED",
    "CUSTOMER_HEALTH_LOW", "PROJECT_BLOCKED", "COMPLIANCE_DEADLINE",
})


@dataclass(frozen=True)
class EscalationRuleSpec:
    rule_id: str
    type: str
    severity: str
    escalate_to: str
    create_task: bool
    params: Mapping[str, Any]


@dataclass(frozen=True)
class PrivacyGate:
    tool_name: str
    approved: bool
    reviewed_by: str | None
    reviewed_at: str | None
    allowed_fields: tuple[str, ...]


@dataclass(frozen=True)
class AttachmentPolicy:
    allowed_mime_types: frozenset[str] = frozenset()
    max_size_bytes: int = 0
    model_visible_classifications: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OrganizationPolicy:
    organization_id: str
    name: str
    currency: str
    timezone: str
    global_monetary: MonetaryRule | None
    monetary: Mapping[str, MonetaryRule]
    recipients: Mapping[str, RecipientRule]
    sensitive_fields: Mapping[str, SensitiveFieldRule]
    ttl_hours: Mapping[RiskClass, float]
    max_ttl_hours: Mapping[RiskClass, float]
    default_approver_roles: Mapping[RiskClass, str]
    tool_approver_roles: Mapping[str, Mapping[RiskClass, str]]
    amber_auto_execute_tools: frozenset[str]
    orchestration: OrchestrationLimits = field(default_factory=OrchestrationLimits)
    budgets: BudgetPolicy = CONSERVATIVE_BUDGETS
    scheduler: SchedulerPolicy = field(default_factory=SchedulerPolicy)
    context_limits: ContextLimits = field(default_factory=ContextLimits)
    retries: RetryPolicies = field(default_factory=RetryPolicies)
    escalations: tuple[EscalationRuleSpec, ...] = ()
    privacy_gates: Mapping[str, PrivacyGate] = field(default_factory=dict)
    attachments: AttachmentPolicy = field(default_factory=AttachmentPolicy)
    placeholders: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    external_adapters: Mapping[str, Any] = field(default_factory=dict)     # tool -> AdapterSetting (all off)
    briefing: BriefingPolicy = field(default_factory=BriefingPolicy)

    def privacy_gate_open(self, tool_name: str) -> bool:
        gate = self.privacy_gates.get(tool_name)
        return bool(gate and gate.approved and gate.reviewed_by and gate.reviewed_at)

    def approver_role(self, tool_name: str, risk_class: RiskClass) -> str:
        role = self.tool_approver_roles.get(tool_name, {}).get(risk_class)
        if role is None:
            role = self.default_approver_roles.get(risk_class)
        if role is None:
            raise ConfigurationError(f"no approver role configured for {tool_name} at {risk_class.value}")
        return role

    def approval_ttl_hours(self, risk_class: RiskClass) -> float:
        if risk_class not in self.ttl_hours:
            raise ConfigurationError(f"no approval TTL configured for {risk_class.value}")
        return self.ttl_hours[risk_class]

    # ------------------------------------------------------------------------------------
    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], tools: ToolRegistry = DEFAULT_TOOL_REGISTRY) -> "OrganizationPolicy":
        try:
            risk = raw["risk"]
            approvals = raw["approvals"]

            def monetary(m: Mapping[str, Any], label: str) -> MonetaryRule:
                rule = MonetaryRule(m["amount_field"], m["currency_field"], _dec(m["amber_at"], f"{label}.amber_at"),
                                    _dec(m["red_at"], f"{label}.red_at"), bool(m.get("required", True)))
                if rule.red_at < rule.amber_at:
                    raise ConfigurationError(f"{label}: red_at must be >= amber_at")
                return rule

            def check_tool_or_proposed(name: str) -> str:
                from .tools import PROPOSED_TOOLS
                if not tools.exists(name) and name not in PROPOSED_TOOLS:
                    raise ConfigurationError(f"privacy_gates: unknown tool {name!r}")
                return name

            def check_tool(name: str, where: str) -> str:
                if not tools.exists(name):
                    raise ConfigurationError(f"{where}: unknown tool {name!r}")
                return name

            def risk_map(m: Mapping[str, Any], where: str) -> dict[RiskClass, Any]:
                return {RiskClass(k): v for k, v in m.items()}

            orch = raw.get("orchestration", {})
            policy = cls(
                organization_id=raw["organization_id"],
                name=raw["name"],
                currency=raw["currency"],
                timezone=raw["timezone"],
                global_monetary=monetary(risk["global_monetary"], "global_monetary") if risk.get("global_monetary") else None,
                monetary={check_tool(k, "risk.monetary"): monetary(v, f"monetary.{k}")
                          for k, v in (risk.get("monetary") or {}).items()},
                recipients={check_tool(k, "risk.recipients"): RecipientRule(v["field"], int(v["red_at_count"]))
                            for k, v in (risk.get("recipients") or {}).items()},
                sensitive_fields={check_tool(k, "risk.sensitive_fields"):
                                  SensitiveFieldRule(v["field"], frozenset(v["red_if_keys"]))
                                  for k, v in (risk.get("sensitive_fields") or {}).items()},
                ttl_hours=risk_map(approvals["ttl_hours"], "ttl_hours"),
                max_ttl_hours=risk_map(approvals["max_ttl_hours"], "max_ttl_hours"),
                default_approver_roles=risk_map(approvals["approver_roles"]["default"], "approver_roles.default"),
                tool_approver_roles={check_tool(k, "approver_roles.tools"): risk_map(v, k)
                                     for k, v in (approvals["approver_roles"].get("tools") or {}).items()},
                amber_auto_execute_tools=frozenset(check_tool(t, "amber_auto_execute_tools")
                                                   for t in raw.get("amber_auto_execute_tools") or []),
                orchestration=OrchestrationLimits(**orch) if orch else OrchestrationLimits(),
                budgets=_budgets(raw.get("budgets")),
                scheduler=SchedulerPolicy(**raw["scheduler"]) if raw.get("scheduler") else SchedulerPolicy(),
                context_limits=ContextLimits(**raw["context_limits"]) if raw.get("context_limits") else ContextLimits(),
                retries=_retries(raw.get("retries")),
                escalations=_escalations(raw.get("escalations") or []),
                privacy_gates={check_tool_or_proposed(k): PrivacyGate(k, bool(v.get("approved")), v.get("reviewed_by"),
                                                                      v.get("reviewed_at"),
                                                                      tuple(v.get("allowed_fields") or ()))
                               for k, v in (raw.get("privacy_gates") or {}).items()},
                attachments=AttachmentPolicy(frozenset(raw["attachments"]["allowed_mime_types"]),
                                             int(raw["attachments"]["max_size_bytes"]),
                                             frozenset(raw["attachments"]["model_visible_classifications"]))
                if raw.get("attachments") else AttachmentPolicy(),
                placeholders=tuple(raw.get("placeholders") or ()),
                raw=dict(raw),
                external_adapters=_adapters(raw.get("external_adapters")),
                briefing=_briefing(raw.get("briefing")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ConfigurationError):
                raise
            raise ConfigurationError(f"invalid organization policy: {exc!r}") from exc

        for t in policy.amber_auto_execute_tools:
            if tools.get(t).default_risk_class is RiskClass.RED:
                raise ConfigurationError(f"RED tool {t!r} can never be auto-executed")
        for rc in RiskClass:
            if rc not in policy.ttl_hours or rc not in policy.max_ttl_hours:
                raise ConfigurationError(f"approvals.ttl_hours and max_ttl_hours must cover {rc.value} "
                                         "(every approval expires; no permanent RED approvals)")
        for rc, ttl in policy.ttl_hours.items():
            if ttl <= 0 or ttl > policy.max_ttl_hours.get(rc, ttl):
                raise ConfigurationError(f"approval TTL for {rc.value} must be positive and within max_ttl_hours")
        for ph in policy.placeholders:
            if resolve_dotted(raw, ph) is _MISSING:
                raise ConfigurationError(f"placeholders: {ph!r} does not refer to a policy field")
        if policy.orchestration.max_delegation_depth < 0:
            raise ConfigurationError("max_delegation_depth must be >= 0")
        return policy

    @classmethod
    def load(cls, path: "str | Path | Resource" = DEFAULT_POLICY_PATH,
             tools: ToolRegistry = DEFAULT_TOOL_REGISTRY) -> "OrganizationPolicy":
        path = as_resource(path)
        if not path.is_file():
            raise ConfigurationError(f"organization policy not found: {path}")
        return cls.from_mapping(yaml.safe_load(path.read_text(encoding="utf-8")), tools)


_MISSING = object()


def resolve_dotted(raw: Any, path: str) -> Any:
    """Resolve a dotted path; list elements are addressed by their `rule_id` (or `job_id`)."""
    node = raw
    for part in path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list):
            match = [x for x in node if isinstance(x, Mapping) and part in (x.get("rule_id"), x.get("job_id"))]
            if not match:
                return _MISSING
            node = match[0]
        else:
            return _MISSING
    return node


def _adapters(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    from .adapters import adapter_settings_from_policy
    try:
        return adapter_settings_from_policy(raw)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from None


def _briefing(raw: Mapping[str, Any] | None) -> BriefingPolicy:
    if not raw:
        return BriefingPolicy()
    try:
        b = BriefingPolicy(raw["daily"]["data_agent"], tuple(raw["daily"]["specialists"]),
                           tuple(raw["weekly"]["core"]), dict(raw["weekly"].get("conditional") or {}))
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(f"briefing section invalid: {exc!r}") from None
    for agent, cond in b.weekly_conditional.items():
        if cond not in BRIEFING_CONDITIONS:
            raise ConfigurationError(f"briefing.weekly.conditional.{agent}: unknown condition {cond!r}")
    people = "people_performance"
    if people in b.daily_specialists or people in b.weekly_core or \
            (people in b.weekly_conditional and b.weekly_conditional[people] != "PEOPLE_AUTHORIZED"):
        raise ConfigurationError("people_performance may join briefs only under PEOPLE_AUTHORIZED (privacy gate)")
    return b


def _budgets(raw: Mapping[str, Any] | None) -> BudgetPolicy:
    if not raw:
        return CONSERVATIVE_BUDGETS
    b = BudgetPolicy(raw["currency"], raw.get("organization_max_cost_per_day"),
                     raw.get("organization_max_cost_per_month"), float(raw["warn_at_fraction"]),
                     ExhaustionPolicy(raw["exhaustion_policy"]), raw.get("override_role"),
                     float(raw.get("override_max_hours", 24)))
    if not 0 < b.warn_at_fraction <= 1:
        raise ConfigurationError("budgets.warn_at_fraction must be in (0, 1]")
    if b.exhaustion_policy is ExhaustionPolicy.REQUIRE_OVERRIDE and not b.override_role:
        raise ConfigurationError("budgets.override_role is required for REQUIRE_OVERRIDE")
    return b


def _retries(raw: Mapping[str, Any] | None) -> RetryPolicies:
    if not raw:
        return RetryPolicies()
    r = RetryPolicies(**{k: RetrySetting(int(v["max_attempts"]), float(v["backoff_seconds"])) for k, v in raw.items()})
    for name in ("model", "read_tool", "write_tool"):
        if getattr(r, name).max_attempts < 1:
            raise ConfigurationError(f"retries.{name}.max_attempts must be >= 1")
    return r


def _escalations(raw: list[Mapping[str, Any]]) -> tuple[EscalationRuleSpec, ...]:
    out, seen = [], set()
    for r in raw:
        for k in ("rule_id", "type", "severity", "escalate_to", "create_task"):
            if k not in r:
                raise ConfigurationError(f"escalation rule missing {k!r}: {dict(r)}")
        if r["type"] not in ESCALATION_TYPES:
            raise ConfigurationError(f"unknown escalation type {r['type']!r}")
        if r["severity"] not in ("INFO", "WATCH", "WARNING", "CRITICAL"):
            raise ConfigurationError(f"escalation {r['rule_id']}: invalid severity")
        if r["rule_id"] in seen:
            raise ConfigurationError(f"duplicate escalation rule_id {r['rule_id']!r}")
        seen.add(r["rule_id"])
        params = {k: v for k, v in r.items() if k not in ("rule_id", "type", "severity", "escalate_to", "create_task")}
        out.append(EscalationRuleSpec(r["rule_id"], r["type"], r["severity"], r["escalate_to"],
                                      bool(r["create_task"]), params))
    return tuple(out)
