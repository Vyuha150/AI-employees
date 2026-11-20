"""Organization policy: thresholds, approver roles, TTLs and orchestration limits.

All numbers come from organization configuration. Nothing here assumes a currency or market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import yaml

from .common import RiskClass
from .errors import ConfigurationError
from .tools import DEFAULT_TOOL_REGISTRY, ToolRegistry

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "organization_policy.example.yaml"


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
        if policy.orchestration.max_delegation_depth < 0:
            raise ConfigurationError("max_delegation_depth must be >= 0")
        return policy

    @classmethod
    def load(cls, path: str | Path = DEFAULT_POLICY_PATH, tools: ToolRegistry = DEFAULT_TOOL_REGISTRY) -> "OrganizationPolicy":
        path = Path(path)
        if not path.is_file():
            raise ConfigurationError(f"organization policy not found: {path}")
        return cls.from_mapping(yaml.safe_load(path.read_text(encoding="utf-8")), tools)
