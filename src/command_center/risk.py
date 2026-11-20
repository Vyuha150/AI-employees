"""Risk policy engine (Part 5).

The application classifies every action. The agent's declared risk class is advisory:
  final = max(tool floor, payload escalation, agent declaration)
so a declaration can only ever raise risk. A declaration lower than policy is recorded as
`risk.overridden_by_policy`; one higher than policy is kept (safer) and recorded as
`risk.declared_higher_than_policy`.

Approval requirements:
  RED    -> always requires explicit authorized human approval; never auto-executed.
  AMBER  -> approval, unless the agent is ELEVATED AND the organization lists the tool in
            amber_auto_execute_tools AND the payload did not escalate it.
  GREEN  -> may auto-execute only when the agent's autonomy allows it, the tool is not in the
            agent's always_require_approval list, and the triggering principal holds
            `auto_execute:GREEN`. Otherwise it goes to approval (never silently executed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .agents import AgentProfile, AutonomyLevel
from .common import Principal, RiskClass
from .policy import MonetaryRule, OrganizationPolicy
from .tools import DEFAULT_TOOL_REGISTRY, ToolRegistry, ToolSpec


@dataclass(frozen=True)
class RiskContext:
    policy: OrganizationPolicy
    declared_risk_class: RiskClass | None = None
    tools: ToolRegistry = DEFAULT_TOOL_REGISTRY
    # Amount already requested by the same agent with the same tool in the aggregation window.
    # Added to this action's amount for the RED test, so one RED action cannot be split into
    # several smaller ones to avoid approval (§6).
    prior_committed_amount: Decimal = Decimal(0)


@dataclass(frozen=True)
class RiskAssessment:
    tool_name: str
    tool_floor: RiskClass
    policy_class: RiskClass             # floor + payload escalation (what the application determined)
    declared_class: RiskClass | None    # what the agent claimed
    final_class: RiskClass              # max(policy_class, declared_class)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    escalated_by_payload: bool = False

    @property
    def declared_lower_than_policy(self) -> bool:
        return self.declared_class is not None and self.declared_class.rank < self.policy_class.rank

    @property
    def declared_higher_than_policy(self) -> bool:
        return self.declared_class is not None and self.declared_class.rank > self.policy_class.rank


@dataclass(frozen=True)
class ApprovalDecision:
    required: bool
    reason: str
    required_role: str | None = None


def _amount(payload: Mapping[str, Any], rule: MonetaryRule) -> Decimal | None:
    if rule.amount_field not in payload:
        return None
    raw = payload[rule.amount_field]
    if isinstance(raw, bool):
        raise ValueError("boolean is not an amount")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{rule.amount_field!r} is not numeric") from None
    if not value.is_finite():
        raise ValueError(f"{rule.amount_field!r} is not finite")
    return value


def _apply_monetary(payload: Mapping[str, Any], rule: MonetaryRule, currency: str, label: str,
                    reasons: list[str], prior: Decimal = Decimal(0)) -> RiskClass:
    try:
        amount = _amount(payload, rule)
    except ValueError as exc:
        reasons.append(f"{label}: {exc}; cannot assess value -> RED")
        return RiskClass.RED
    if amount is None:
        if rule.required:
            reasons.append(f"{label}: no {rule.amount_field!r} in payload; cannot assess value -> RED")
            return RiskClass.RED
        return RiskClass.GREEN
    cur = payload.get(rule.currency_field)
    if cur != currency:
        reasons.append(f"{label}: currency {cur!r} differs from organization currency {currency!r} -> RED")
        return RiskClass.RED
    if amount < 0:
        reasons.append(f"{label}: negative amount -> RED")
        return RiskClass.RED
    if amount >= rule.red_at:
        reasons.append(f"{label}: amount {amount} >= RED threshold {rule.red_at} {currency}")
        return RiskClass.RED
    if prior > 0 and amount + prior >= rule.red_at:
        reasons.append(f"{label}: cumulative amount {amount + prior} (this {amount} + prior {prior}) "
                       f">= RED threshold {rule.red_at} {currency} (split-action protection)")
        return RiskClass.RED
    if amount >= rule.amber_at and amount > 0:
        reasons.append(f"{label}: amount {amount} >= AMBER threshold {rule.amber_at} {currency}")
        return RiskClass.AMBER
    return RiskClass.GREEN


def classify_action(tool_name: str, payload: Mapping[str, Any], context: RiskContext) -> RiskAssessment:
    """Classify an action. Raises UnknownToolError for unknown tools (never guesses)."""
    spec: ToolSpec = context.tools.get(tool_name)
    policy = context.policy
    reasons: list[str] = [f"tool floor {spec.default_risk_class.value}"]
    escalation = RiskClass.GREEN

    if not isinstance(payload, Mapping):
        reasons.append("payload is not an object -> RED")
        escalation = RiskClass.RED
    else:
        if tool_name in policy.monetary:
            escalation = RiskClass.max(escalation, _apply_monetary(
                payload, policy.monetary[tool_name], policy.currency, f"monetary rule {tool_name}", reasons,
                context.prior_committed_amount))
        elif policy.global_monetary and policy.global_monetary.amount_field in payload:
            escalation = RiskClass.max(escalation, _apply_monetary(
                payload, policy.global_monetary, policy.currency, "global monetary rule", reasons,
                context.prior_committed_amount))

        if tool_name in policy.recipients:
            rule = policy.recipients[tool_name]
            recips = payload.get(rule.field)
            if not isinstance(recips, (list, tuple)) or not recips:
                reasons.append(f"recipient rule: {rule.field!r} missing or empty -> RED")
                escalation = RiskClass.RED
            elif len(recips) >= rule.red_at_count:
                reasons.append(f"recipient rule: {len(recips)} recipients >= {rule.red_at_count} (bulk) -> RED")
                escalation = RiskClass.RED

        if tool_name in policy.sensitive_fields:
            rule = policy.sensitive_fields[tool_name]
            changes = payload.get(rule.field)
            if not isinstance(changes, Mapping):
                reasons.append(f"sensitive-field rule: {rule.field!r} missing or not an object -> RED")
                escalation = RiskClass.RED
            else:
                hit = sorted(set(changes) & rule.red_if_keys)
                if hit:
                    reasons.append(f"sensitive-field rule: changes {', '.join(hit)} -> RED")
                    escalation = RiskClass.RED

    policy_class = RiskClass.max(spec.default_risk_class, escalation)
    declared = context.declared_risk_class
    final = policy_class if declared is None else RiskClass.max(policy_class, declared)
    if declared is not None and declared.rank < policy_class.rank:
        reasons.append(f"agent declared {declared.value}; policy classification {policy_class.value} wins")
    elif declared is not None and declared.rank > policy_class.rank:
        reasons.append(f"agent declared {declared.value} above policy {policy_class.value}; safer class retained")
    return RiskAssessment(tool_name, spec.default_risk_class, policy_class, declared, final, tuple(reasons),
                          escalated_by_payload=policy_class.rank > spec.default_risk_class.rank)


def validate_risk_class(declared: RiskClass | None, assessment: RiskAssessment) -> str | None:
    """Return a discrepancy description, or None when the declaration matches policy."""
    if declared is None:
        return "agent did not declare a risk class"
    if declared is assessment.policy_class:
        return None
    direction = "lower" if declared.rank < assessment.policy_class.rank else "higher"
    return f"declared {declared.value} is {direction} than policy {assessment.policy_class.value}"


def requires_approval(
    assessment: RiskAssessment,
    agent: AgentProfile,
    policy: OrganizationPolicy,
    principal: Principal,
) -> ApprovalDecision:
    risk = assessment.final_class
    role = policy.approver_role(assessment.tool_name, risk)
    if risk is RiskClass.RED:
        return ApprovalDecision(True, "RED actions always require explicit authorized human approval", role)
    if agent.autonomy_level is AutonomyLevel.ADVISORY:
        return ApprovalDecision(True, "agent autonomy is ADVISORY", role)
    if assessment.tool_name in agent.always_require_approval:
        return ApprovalDecision(True, "agent profile requires approval for this tool", role)
    if risk is RiskClass.AMBER:
        if (agent.autonomy_level is AutonomyLevel.ELEVATED
                and assessment.tool_name in policy.amber_auto_execute_tools
                and not assessment.escalated_by_payload
                and principal.has("auto_execute:AMBER")):
            return ApprovalDecision(False, "AMBER auto-execution permitted by organization policy")
        return ApprovalDecision(True, "AMBER actions require approval", role)
    if not principal.has("auto_execute:GREEN"):
        return ApprovalDecision(True, "triggering principal lacks auto_execute:GREEN", role)
    return ApprovalDecision(False, "GREEN action within agent autonomy and principal permissions")
