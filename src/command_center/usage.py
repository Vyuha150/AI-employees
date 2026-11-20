"""AI usage accounting (Part 51) and budget enforcement (Part 52).

- Cost = tokens x provider-configured price. Missing pricing (or missing cache pricing when
  cache tokens were used) stores cost NULL with an explanation. Nothing is estimated.
- Budgets: per run (agent cost_limits, enforced by RunService), per agent per day and per
  month (agent cost_limits), per organization per day and month (organization budgets).
- On exhaustion the organization policy decides: WARN (continue, audited),
  BLOCK_NEW_RUNS (refuse new runs), REQUIRE_OVERRIDE (refuse unless a human with
  budgets.override_role granted a time-boxed override). Overrides are audited.
- Spend with unknown cost cannot be summed; it is reported as `unpriced_usage` so budgets
  are never silently treated as unspent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .agents import AgentProfile
from .audit import AuditLogger, EventType
from .common import ActorType, Clock, Principal, new_id, utc_now
from .errors import BudgetExceededError, PermissionDeniedError, ValidationFailedError
from .policy import ExhaustionPolicy, OrganizationPolicy
from .storage import InMemoryRepository, Repository

MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    def total(self) -> int:
        return self.input_tokens + self.output_tokens + (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0)


@dataclass(frozen=True)
class UsageRecord:
    usage_id: str
    organization_id: str
    provider: str
    model: str
    agent_id: str
    run_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    tool_calls: int
    estimated_cost: Decimal | None
    cost_explanation: str
    currency: str | None
    timestamp: datetime
    fallback_used: bool = False
    version: int = 0


def compute_cost(pricing: Any, usage: TokenUsage) -> tuple[Decimal | None, str, str | None]:
    """pricing: config.ModelPricing or None. Returns (cost, explanation, currency)."""
    if pricing is None:
        return None, "no pricing configured for this provider/model; cost not determined", None
    cost = (Decimal(usage.input_tokens) * Decimal(str(pricing.input_per_mtok))
            + Decimal(usage.output_tokens) * Decimal(str(pricing.output_per_mtok))) / MTOK
    for tokens, price, label in ((usage.cache_read_tokens, pricing.cache_read_per_mtok, "cache read"),
                                 (usage.cache_write_tokens, pricing.cache_write_per_mtok, "cache write")):
        if tokens:
            if price is None:
                return None, f"{label} tokens used but no {label} price configured; cost not determined", None
            cost += Decimal(tokens) * Decimal(str(price)) / MTOK
    return cost.quantize(Decimal("0.000001")), f"priced from configuration as of {pricing.as_of}", pricing.currency


@dataclass(frozen=True)
class BudgetOverride:
    override_id: str
    organization_id: str
    scope: str                 # agent id, or "ORGANIZATION"
    granted_by: str
    reason: str
    expires_at: datetime


@dataclass(frozen=True)
class SpendSummary:
    known_cost: Decimal
    unpriced_usage: int        # usage records whose cost is NULL


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    warnings: tuple[str, ...]
    exceeded: tuple[str, ...]


@dataclass
class UsageService:
    policy: OrganizationPolicy
    audit: AuditLogger
    repo: Repository[UsageRecord] = field(default_factory=lambda: InMemoryRepository("usage_id"))
    overrides: list[BudgetOverride] = field(default_factory=list)
    clock: Clock = utc_now

    def record(self, *, run_id: str, agent_id: str, provider: str, model: str, usage: TokenUsage, tool_calls: int,
               pricing: Any, fallback_used: bool = False) -> UsageRecord:
        cost, why, currency = compute_cost(pricing, usage)
        rec = UsageRecord(new_id("use"), self.policy.organization_id, provider, model, agent_id, run_id,
                          usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
                          tool_calls, cost, why, currency, self.clock(), fallback_used)
        self.repo.add(rec)
        self.audit.record(EventType.USAGE_RECORDED, organization_id=rec.organization_id, actor_type=ActorType.SYSTEM,
                          actor_id="usage", agent_id=agent_id, run_id=run_id, object_type="ai_usage",
                          object_id=rec.usage_id,
                          payload={"provider": provider, "model": model, "input_tokens": usage.input_tokens,
                                   "output_tokens": usage.output_tokens, "cache_read_tokens": usage.cache_read_tokens,
                                   "cache_write_tokens": usage.cache_write_tokens, "tool_calls": tool_calls,
                                   "estimated_cost": None if cost is None else str(cost), "cost_explanation": why})
        return rec

    # -- spend ----------------------------------------------------------------------------
    def spend(self, *, since: datetime, agent_id: str | None = None) -> SpendSummary:
        rows = self.repo.list(lambda r: r.timestamp >= since and (agent_id is None or r.agent_id == agent_id))
        known = sum((r.estimated_cost for r in rows if r.estimated_cost is not None), Decimal(0))
        return SpendSummary(known, sum(1 for r in rows if r.estimated_cost is None))

    def _periods(self) -> tuple[datetime, datetime]:
        now = self.clock()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day, day.replace(day=1)

    # -- enforcement ----------------------------------------------------------------------
    def check_can_start(self, agent: AgentProfile) -> BudgetDecision:
        """Raise BudgetExceededError when the policy blocks a new run; otherwise return warnings."""
        day, month = self._periods()
        b = self.policy.budgets
        checks = [
            (f"agent {agent.agent_id} daily", self.spend(since=day, agent_id=agent.agent_id), agent.cost_limits.max_cost_per_day),
            (f"agent {agent.agent_id} monthly", self.spend(since=month, agent_id=agent.agent_id), agent.cost_limits.max_cost_per_month),
            ("organization daily", self.spend(since=day), b.organization_max_cost_per_day),
            ("organization monthly", self.spend(since=month), b.organization_max_cost_per_month),
        ]
        warnings, exceeded = [], []
        for label, s, limit in checks:
            if limit is None:
                continue
            if s.unpriced_usage:
                warnings.append(f"{label}: {s.unpriced_usage} usage record(s) have unknown cost")
            if s.known_cost >= Decimal(str(limit)):
                exceeded.append(f"{label} budget {limit} {b.currency} reached (spent {s.known_cost})")
            elif s.known_cost >= Decimal(str(limit)) * Decimal(str(b.warn_at_fraction)):
                warnings.append(f"{label} budget at {s.known_cost}/{limit} {b.currency}")
        org = self.policy.organization_id
        for w in warnings:
            self.audit.record(EventType.BUDGET_WARNING, organization_id=org, actor_type=ActorType.SYSTEM,
                              actor_id="budget", agent_id=agent.agent_id, object_type="budget", object_id=agent.agent_id,
                              payload={"warning": w})
        if not exceeded:
            return BudgetDecision(True, tuple(warnings), ())
        self.audit.record(EventType.BUDGET_EXCEEDED, organization_id=org, actor_type=ActorType.SYSTEM, actor_id="budget",
                          agent_id=agent.agent_id, object_type="budget", object_id=agent.agent_id,
                          payload={"exceeded": exceeded, "policy": b.exhaustion_policy.value})
        if b.exhaustion_policy is ExhaustionPolicy.WARN:
            return BudgetDecision(True, tuple(warnings) + tuple(exceeded), tuple(exceeded))
        if b.exhaustion_policy is ExhaustionPolicy.REQUIRE_OVERRIDE and self._active_override(agent.agent_id):
            return BudgetDecision(True, tuple(warnings) + ("running under a budget override",), tuple(exceeded))
        raise BudgetExceededError("; ".join(exceeded) + f" (policy {b.exhaustion_policy.value})")

    def _active_override(self, agent_id: str) -> BudgetOverride | None:
        now = self.clock()
        for o in self.overrides:
            if o.expires_at > now and o.scope in (agent_id, "ORGANIZATION"):
                return o
        return None

    def grant_override(self, principal: Principal, *, scope: str, reason: str, hours: float) -> BudgetOverride:
        b = self.policy.budgets
        if b.exhaustion_policy is not ExhaustionPolicy.REQUIRE_OVERRIDE:
            raise ValidationFailedError("overrides apply only under the REQUIRE_OVERRIDE policy")
        if not principal.is_human or principal.organization_id != self.policy.organization_id \
                or b.override_role not in principal.roles:
            raise PermissionDeniedError(f"budget overrides require a human with role {b.override_role!r}")
        if not reason.strip() or hours <= 0 or hours > b.override_max_hours:
            raise ValidationFailedError(f"override needs a reason and 0 < hours <= {b.override_max_hours}")
        o = BudgetOverride(new_id("bov"), self.policy.organization_id, scope, principal.principal_id, reason,
                           self.clock() + timedelta(hours=hours))
        self.overrides.append(o)
        self.audit.record(EventType.BUDGET_OVERRIDE_GRANTED, organization_id=o.organization_id,
                          actor_type=ActorType.HUMAN, actor_id=principal.principal_id, object_type="budget",
                          object_id=o.override_id, payload={"scope": scope, "reason": reason,
                                                            "expires_at": o.expires_at.isoformat()})
        return o
