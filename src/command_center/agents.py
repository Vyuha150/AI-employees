"""Central agent registry (Part 3).

Fail-closed rules
- unknown agent_id                -> UnknownAgentError
- on the roster but no file       -> AgentProfileMissingError
- profile failed validation       -> InvalidAgentProfileError (with every error listed)
- profile has enabled: false      -> AgentDisabledError
No generic or fallback agent is ever substituted.

Static profile vs run context
A profile holds the 11 static prompt fields. The 6 per-run fields (organization context,
memory, knowledge, task, user, user permissions) come from a `RunContext`, which is mandatory
and has no defaults, so a user's permissions are never read from static configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .common import AgentRole, Principal, StrEnum
from .errors import (
    AgentDisabledError,
    AgentProfileMissingError,
    ConfigurationError,
    InvalidAgentProfileError,
    UnknownAgentError,
)
from .resources import Resource, as_resource, default_agent_dir, stem
from .tools import DEFAULT_TOOL_REGISTRY, ToolRegistry

DEFAULT_AGENT_DIR = default_agent_dir()  # packaged defaults (importlib.resources)

# The 12 core agents. A missing file for any of these is an error, not a silent gap.
CORE_ROSTER: tuple[str, ...] = (
    "chief_of_staff", "sales_director", "lead_generation", "marketing_director", "cfo", "operations",
    "customer_success", "product_analyst", "procurement", "data_analyst", "legal_risk", "people_performance",
)


class AutonomyLevel(StrEnum):
    """What an agent may execute without a per-action human approval. RED is never autonomous."""

    ADVISORY = "ADVISORY"    # nothing auto-executes; every state change needs approval
    MODERATE = "MODERATE"    # GREEN may auto-execute; AMBER/RED need approval
    ELEVATED = "ELEVATED"    # as MODERATE, plus AMBER tools the ORGANIZATION policy lists for auto-execution


class MemoryScope(StrEnum):
    ORGANIZATION = "ORGANIZATION"
    DEPARTMENT = "DEPARTMENT"
    AGENT = "AGENT"
    CUSTOMER = "CUSTOMER"
    PROJECT = "PROJECT"


class KpiDirection(StrEnum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    TARGET_RANGE = "TARGET_RANGE"


@dataclass(frozen=True)
class KpiDefinition:
    name: str
    definition: str
    direction: KpiDirection
    unit: str


@dataclass(frozen=True)
class EscalationRule:
    trigger: str
    escalate_to: str
    within_hours: float


@dataclass(frozen=True)
class CostLimits:
    max_cost_per_run: float
    max_tokens_per_run: int
    max_runs_per_day: int
    currency: str
    max_cost_per_day: float
    max_cost_per_month: float


class MissedRunPolicy(StrEnum):
    SKIP = "SKIP"            # default: missed occurrences are recorded and skipped
    RUN_ONCE = "RUN_ONCE"    # one run for the most recent missed occurrence
    CATCH_UP = "CATCH_UP"    # one run per missed occurrence, bounded by scheduler.max_catch_up_runs


class ConcurrencyPolicy(StrEnum):
    FORBID = "FORBID"        # default: do not start while a previous run of the job is active
    ALLOW = "ALLOW"


@dataclass(frozen=True)
class ScheduledJobSpec:
    job_id: str
    workflow: str
    cron: str
    timezone: str            # IANA name, or ORGANIZATION_DEFAULT
    enabled: bool
    missed_run_policy: MissedRunPolicy
    concurrency_policy: ConcurrencyPolicy


@dataclass(frozen=True)
class DelegationPolicy:
    can_delegate: bool
    allowed_targets: tuple[str, ...]


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    name: str
    role: AgentRole
    department: str
    manager: str
    reports_to: str | None
    enabled: bool
    mission: str
    objectives: tuple[str, ...]
    responsibilities: tuple[str, ...]
    kpis: tuple[KpiDefinition, ...]
    allowed_tools: tuple[str, ...]
    autonomy_level: AutonomyLevel
    autonomy_description: str
    risk_policy_summary: str
    always_require_approval: tuple[str, ...]
    escalation_rules: tuple[EscalationRule, ...]
    knowledge_scopes: tuple[str, ...]
    memory_scopes: tuple[MemoryScope, ...]
    schedule_policy: Mapping[str, Any]
    cost_limits: CostLimits
    output_preferences: Mapping[str, Any]
    prohibited_actions: tuple[str, ...]
    boundaries: tuple[str, ...]
    delegation: DelegationPolicy
    model_profile: str = "standard"
    schedule_jobs: tuple[ScheduledJobSpec, ...] = ()
    triggers: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()

    def to_static_prompt_fields(self) -> dict[str, Any]:
        return {
            "AGENT_ID": self.agent_id,
            "AGENT_NAME": self.name,
            "AGENT_ROLE": self.role.value,
            "DEPARTMENT": self.department,
            "MANAGER": self.manager,
            "MISSION": self.mission,
            "OBJECTIVES": list(self.objectives),
            "KPIS": [f"{k.name} ({k.unit}, {k.direction.value.lower().replace('_', ' ')}): {k.definition}"
                     for k in self.kpis],
            "ALLOWED_TOOLS": list(self.allowed_tools),
            "AUTONOMY_LEVEL": f"{self.autonomy_level.value} — {self.autonomy_description}",
            "RISK_POLICY": self.risk_policy_summary + (
                f" Always require approval for: {', '.join(self.always_require_approval)}."
                if self.always_require_approval else ""),
            # extended (optional) prompt fields
            "RESPONSIBILITIES": list(self.responsibilities),
            "ESCALATION_RULES": [f"{r.trigger} -> escalate to {r.escalate_to} within {r.within_hours:g}h"
                                 for r in self.escalation_rules],
            "PROHIBITED_ACTIONS": list(self.prohibited_actions),
            "BOUNDARIES": list(self.boundaries),
            "DELEGATION": ("May delegate to: " + ", ".join(self.delegation.allowed_targets))
            if self.delegation.can_delegate else "Must not delegate to other agents.",
            "OUTPUT_PREFERENCES": dict(self.output_preferences),
        }


@dataclass(frozen=True)
class RunContext:
    """Per-run context (Part 31). Kept separate from static agent configuration on purpose.

    The first five fields supply the dynamic prompt fields and are required. The request
    identity fields are optional metadata. User identity, roles and permissions are read
    from `current_user` (the authenticated Principal); they are never copied into YAML.
    """

    organization_context: str
    relevant_memory: tuple[str, ...]
    relevant_knowledge: tuple[str, ...]
    current_task: str
    current_user: Principal
    conversation_id: str | None = field(default=None, kw_only=True)
    task_id: str | None = field(default=None, kw_only=True)
    workflow_run_id: str | None = field(default=None, kw_only=True)
    request_id: str | None = field(default=None, kw_only=True)
    timezone: str | None = field(default=None, kw_only=True)          # None: organization default
    requested_agent_id: str | None = field(default=None, kw_only=True)
    request_timestamp: datetime | None = field(default=None, kw_only=True)

    @property
    def organization_id(self) -> str:
        return self.current_user.organization_id

    @property
    def user_id(self) -> str:
        return self.current_user.principal_id

    @property
    def user_roles(self) -> frozenset[str]:
        return self.current_user.roles

    @property
    def user_permissions(self) -> frozenset[str]:
        return self.current_user.permissions

    def to_prompt_fields(self) -> dict[str, Any]:
        if not self.current_task.strip():
            raise ConfigurationError("RunContext.current_task must not be empty")
        return {
            "ORGANIZATION_CONTEXT": self.organization_context,
            "RELEVANT_MEMORY": list(self.relevant_memory),
            "RELEVANT_KNOWLEDGE": list(self.relevant_knowledge),
            "CURRENT_TASK": self.current_task,
            "CURRENT_USER": self.current_user.principal_id,
            "CURRENT_USER_PERMISSIONS": sorted(self.current_user.permissions),
        }


def build_prompt_profile(agent: AgentProfile, context: RunContext) -> dict[str, Any]:
    return {**agent.to_static_prompt_fields(), **context.to_prompt_fields()}


# ----------------------------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------------------------
_REQUIRED = {
    "agent_id": str, "name": str, "role": str, "department": str, "manager": str, "enabled": bool,
    "mission": str, "objectives": list, "responsibilities": list, "kpis": list, "allowed_tools": list,
    "autonomy_level": str, "autonomy_description": str, "risk_policy": dict, "escalation_rules": list,
    "knowledge_scopes": list, "memory_scopes": list, "schedule_policy": dict, "cost_limits": dict,
    "output_preferences": dict, "prohibited_actions": list, "boundaries": list, "delegation": dict,
    "model_profile": str,
}
_OPTIONAL = {"reports_to", "placeholders"}
_COST_KEYS = {"max_cost_per_run", "max_cost_per_day", "max_cost_per_month", "max_tokens_per_run",
              "max_runs_per_day", "currency"}
_JOB_KEYS = {"job_id", "workflow", "cron", "timezone", "enabled", "missed_run_policy", "concurrency_policy"}
_TRIGGERS = {"scheduled", "on_demand", "delegated"}


def _dotted_exists(raw: Mapping[str, Any], path: str) -> bool:
    node: Any = raw
    for part in path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return False
    return True


def validate_agent_profile(
    raw: Mapping[str, Any],
    tools: ToolRegistry = DEFAULT_TOOL_REGISTRY,
    expected_agent_id: str | None = None,
) -> list[str]:
    """Return every problem with a raw profile mapping. An empty list means valid."""
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        return ["profile must be a mapping"]

    for key, typ in _REQUIRED.items():
        if key not in raw:
            errors.append(f"missing field {key!r}")
        elif not isinstance(raw[key], typ) or (typ is str and not raw[key].strip()):
            errors.append(f"field {key!r} must be a non-empty {typ.__name__}")
    unknown = set(raw) - set(_REQUIRED) - _OPTIONAL
    if unknown:
        errors.append(f"unknown fields: {', '.join(sorted(unknown))}")
    if errors:
        return errors

    if expected_agent_id is not None and raw["agent_id"] != expected_agent_id:
        errors.append(f"agent_id {raw['agent_id']!r} does not match file name {expected_agent_id!r}")

    try:
        role = AgentRole(raw["role"])
    except ValueError:
        errors.append(f"unknown role {raw['role']!r}")
        role = None

    for key in ("objectives", "responsibilities", "knowledge_scopes", "boundaries"):
        if not raw[key] or not all(isinstance(v, str) and v.strip() for v in raw[key]):
            errors.append(f"{key} must be a non-empty list of strings")

    if not raw["kpis"]:
        errors.append("kpis must not be empty")
    for i, k in enumerate(raw["kpis"]):
        if not isinstance(k, Mapping) or set(k) != {"name", "definition", "direction", "unit"}:
            errors.append(f"kpis[{i}] must have exactly name, definition, direction, unit")
            continue
        if k["direction"] not in KpiDirection.__members__:
            errors.append(f"kpis[{i}] has invalid direction {k['direction']!r}")

    allowed = raw["allowed_tools"]
    if len(allowed) != len(set(allowed)):
        errors.append("allowed_tools contains duplicates")
    for t in allowed:
        if not tools.exists(t):
            errors.append(f"allowed_tools: unknown tool {t!r}")
        elif role is not None and not tools.role_may_use(role, t):
            errors.append(f"allowed_tools: role {role.value} may not be granted {t!r}")

    for t in raw["prohibited_actions"]:
        if not tools.exists(t):
            errors.append(f"prohibited_actions: unknown tool {t!r}")
    overlap = set(allowed) & set(raw["prohibited_actions"])
    if overlap:
        errors.append(f"tools both allowed and prohibited: {', '.join(sorted(overlap))}")

    if raw["autonomy_level"] not in AutonomyLevel.__members__:
        errors.append(f"invalid autonomy_level {raw['autonomy_level']!r}")

    rp = raw["risk_policy"]
    if set(rp) != {"summary", "always_require_approval"} or not isinstance(rp.get("summary"), str):
        errors.append("risk_policy must have exactly summary and always_require_approval")
    else:
        for t in rp["always_require_approval"]:
            if t not in allowed:
                errors.append(f"risk_policy.always_require_approval: {t!r} is not an allowed tool")

    for i, r in enumerate(raw["escalation_rules"]):
        if not isinstance(r, Mapping) or set(r) != {"trigger", "escalate_to", "within_hours"}:
            errors.append(f"escalation_rules[{i}] must have exactly trigger, escalate_to, within_hours")
        elif not isinstance(r["within_hours"], (int, float)) or r["within_hours"] <= 0:
            errors.append(f"escalation_rules[{i}].within_hours must be positive")

    for s in raw["memory_scopes"]:
        if s not in MemoryScope.__members__:
            errors.append(f"invalid memory scope {s!r}")
    if not raw["memory_scopes"]:
        errors.append("memory_scopes must not be empty")

    cl = raw["cost_limits"]
    if set(cl) != _COST_KEYS:
        errors.append("cost_limits must have exactly " + ", ".join(sorted(_COST_KEYS)))
    else:
        for k in sorted(_COST_KEYS - {"currency"}):
            if not isinstance(cl[k], (int, float)) or isinstance(cl[k], bool) or cl[k] <= 0:
                errors.append(f"cost_limits.{k} must be a positive number")
        if not errors and not (cl["max_cost_per_run"] <= cl["max_cost_per_day"] <= cl["max_cost_per_month"]):
            errors.append("cost_limits must satisfy per_run <= per_day <= per_month")

    sp = raw["schedule_policy"]
    if set(sp) != {"triggers", "jobs"} or not isinstance(sp.get("jobs"), list):
        errors.append("schedule_policy must have exactly triggers and jobs")
    else:
        bad = set(sp["triggers"]) - _TRIGGERS
        if bad:
            errors.append(f"schedule_policy.triggers has unknown values {sorted(bad)}")
        if sp["jobs"] and "scheduled" not in sp["triggers"]:
            errors.append("schedule_policy has jobs but 'scheduled' is not an allowed trigger")
        seen: set[str] = set()
        for i, j in enumerate(sp["jobs"]):
            if not isinstance(j, Mapping) or set(j) != _JOB_KEYS:
                errors.append(f"schedule_policy.jobs[{i}] must have exactly {', '.join(sorted(_JOB_KEYS))}")
                continue
            if j["job_id"] in seen:
                errors.append(f"duplicate job_id {j['job_id']!r}")
            seen.add(j["job_id"])
            if not isinstance(j["enabled"], bool):
                errors.append(f"schedule_policy.jobs[{i}].enabled must be a boolean")
            if j["missed_run_policy"] not in MissedRunPolicy.__members__:
                errors.append(f"schedule_policy.jobs[{i}] invalid missed_run_policy")
            if j["concurrency_policy"] not in ConcurrencyPolicy.__members__:
                errors.append(f"schedule_policy.jobs[{i}] invalid concurrency_policy")
            from .cron import CronError, CronExpression
            try:
                CronExpression.parse(j["cron"])
            except CronError as exc:
                errors.append(f"schedule_policy.jobs[{i}].cron: {exc}")
            if j["timezone"] != "ORGANIZATION_DEFAULT":
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                try:
                    ZoneInfo(j["timezone"])
                except (ZoneInfoNotFoundError, ValueError):
                    errors.append(f"schedule_policy.jobs[{i}].timezone {j['timezone']!r} is not an IANA zone")

    for ph in raw.get("placeholders", []) or []:
        if not isinstance(ph, str) or not _dotted_exists(raw, ph):
            errors.append(f"placeholders: {ph!r} does not refer to a field in this profile")

    d = raw["delegation"]
    if set(d) != {"can_delegate", "allowed_targets"} or not isinstance(d.get("can_delegate"), bool):
        errors.append("delegation must have exactly can_delegate (bool) and allowed_targets (list)")
    else:
        if d["can_delegate"] and role is not AgentRole.CHIEF_OF_STAFF:
            errors.append("only the Chief of Staff may delegate (specialist delegation is not permitted)")
        if not d["can_delegate"] and d["allowed_targets"]:
            errors.append("allowed_targets must be empty when can_delegate is false")
        if raw["agent_id"] in d["allowed_targets"]:
            errors.append("an agent cannot delegate to itself")

    return errors


def _parse(raw: Mapping[str, Any]) -> AgentProfile:
    cl = raw["cost_limits"]
    return AgentProfile(
        agent_id=raw["agent_id"],
        name=raw["name"],
        role=AgentRole(raw["role"]),
        department=raw["department"],
        manager=raw["manager"],
        reports_to=raw.get("reports_to"),
        enabled=raw["enabled"],
        mission=raw["mission"].strip(),
        objectives=tuple(raw["objectives"]),
        responsibilities=tuple(raw["responsibilities"]),
        kpis=tuple(KpiDefinition(k["name"], k["definition"], KpiDirection(k["direction"]), k["unit"])
                   for k in raw["kpis"]),
        allowed_tools=tuple(raw["allowed_tools"]),
        autonomy_level=AutonomyLevel(raw["autonomy_level"]),
        autonomy_description=raw["autonomy_description"],
        risk_policy_summary=raw["risk_policy"]["summary"],
        always_require_approval=tuple(raw["risk_policy"]["always_require_approval"]),
        escalation_rules=tuple(EscalationRule(r["trigger"], r["escalate_to"], float(r["within_hours"]))
                               for r in raw["escalation_rules"]),
        knowledge_scopes=tuple(raw["knowledge_scopes"]),
        memory_scopes=tuple(MemoryScope(s) for s in raw["memory_scopes"]),
        schedule_policy=dict(raw["schedule_policy"]),
        cost_limits=CostLimits(float(cl["max_cost_per_run"]), int(cl["max_tokens_per_run"]),
                               int(cl["max_runs_per_day"]), cl["currency"], float(cl["max_cost_per_day"]),
                               float(cl["max_cost_per_month"])),
        output_preferences=dict(raw["output_preferences"]),
        prohibited_actions=tuple(raw["prohibited_actions"]),
        boundaries=tuple(raw["boundaries"]),
        delegation=DelegationPolicy(raw["delegation"]["can_delegate"], tuple(raw["delegation"]["allowed_targets"])),
        model_profile=raw["model_profile"],
        schedule_jobs=tuple(ScheduledJobSpec(j["job_id"], j["workflow"], j["cron"], j["timezone"], j["enabled"],
                                             MissedRunPolicy(j["missed_run_policy"]),
                                             ConcurrencyPolicy(j["concurrency_policy"]))
                            for j in raw["schedule_policy"]["jobs"]),
        triggers=tuple(raw["schedule_policy"]["triggers"]),
        placeholders=tuple(raw.get("placeholders") or ()),
    )


def load_raw_profiles(directory: "str | Path | Resource" = DEFAULT_AGENT_DIR) -> dict[str, Any]:
    """Read every *.yaml profile in a directory (filesystem or packaged resource)."""
    directory = as_resource(directory)
    if not directory.is_dir():
        raise ConfigurationError(f"agent config directory not found: {directory}")
    raws: dict[str, Any] = {}
    for node in sorted(directory.iterdir(), key=lambda n: n.name):
        if not node.name.endswith(".yaml"):
            continue
        try:
            raws[stem(node)] = yaml.safe_load(node.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raws[stem(node)] = {"__parse_error__": str(exc)}
    return raws


# ----------------------------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------------------------
@dataclass
class AgentRegistry:
    _profiles: dict[str, AgentProfile] = field(default_factory=dict)
    _invalid: dict[str, list[str]] = field(default_factory=dict)
    _missing: set[str] = field(default_factory=set)

    @classmethod
    def from_mappings(
        cls,
        raws: Mapping[str, Mapping[str, Any]],
        tools: ToolRegistry = DEFAULT_TOOL_REGISTRY,
        roster: Iterable[str] = CORE_ROSTER,
    ) -> "AgentRegistry":
        reg = cls()
        for key, raw in raws.items():
            errs = validate_agent_profile(raw, tools, expected_agent_id=key)
            if errs:
                reg._invalid[key] = errs
            else:
                reg._profiles[key] = _parse(raw)
        reg._missing = set(roster) - set(raws)
        reg._validate_hierarchy()
        return reg

    @classmethod
    def load_dir(
        cls,
        directory: "str | Path | Resource" = DEFAULT_AGENT_DIR,
        tools: ToolRegistry = DEFAULT_TOOL_REGISTRY,
        roster: Iterable[str] = CORE_ROSTER,
    ) -> "AgentRegistry":
        return cls.from_mappings(load_raw_profiles(directory), tools, roster)

    # -- hierarchy ---------------------------------------------------------------------
    def _invalidate(self, agent_id: str, error: str) -> None:
        self._profiles.pop(agent_id, None)
        self._invalid.setdefault(agent_id, []).append(error)

    def _validate_hierarchy(self) -> None:
        known = set(self._profiles) | set(self._invalid) | self._missing
        for aid, p in list(self._profiles.items()):
            if p.reports_to is not None and p.reports_to not in known:
                self._invalidate(aid, f"reports_to {p.reports_to!r} is not a registered agent")
            for t in p.delegation.allowed_targets:
                if t not in known:
                    self._invalidate(aid, f"delegation target {t!r} is not a registered agent")
        for aid in list(self._profiles):
            seen, cur = {aid}, self._profiles.get(aid)
            while cur is not None and cur.reports_to is not None:
                if cur.reports_to in seen:
                    self._invalidate(aid, "reporting line contains a cycle")
                    break
                seen.add(cur.reports_to)
                cur = self._profiles.get(cur.reports_to)

    # -- lookups (fail closed) -----------------------------------------------------------
    def get_agent(self, agent_id: str) -> AgentProfile:
        """Return a profile that is valid AND enabled, i.e. allowed to execute."""
        profile = self.get_profile(agent_id)
        if not profile.enabled:
            raise AgentDisabledError(f"agent {agent_id!r} is disabled")
        return profile

    def get_profile(self, agent_id: str) -> AgentProfile:
        """Return a valid profile regardless of enabled state (for reporting/hierarchy only)."""
        if agent_id in self._profiles:
            return self._profiles[agent_id]
        if agent_id in self._invalid:
            raise InvalidAgentProfileError(f"agent {agent_id!r} has an invalid profile: "
                                           + "; ".join(self._invalid[agent_id]))
        if agent_id in self._missing:
            raise AgentProfileMissingError(f"agent {agent_id!r} is on the roster but has no profile")
        raise UnknownAgentError(f"unknown agent {agent_id!r}")

    def list_agents(self, include_disabled: bool = False) -> list[AgentProfile]:
        return [p for _, p in sorted(self._profiles.items()) if include_disabled or p.enabled]

    def get_agents_by_department(self) -> dict[str, list[AgentProfile]]:
        out: dict[str, list[AgentProfile]] = {}
        for p in self.list_agents(include_disabled=True):
            out.setdefault(p.department, []).append(p)
        return out

    def get_reports_to(self, agent_id: str) -> AgentProfile | None:
        """The agent's manager, or None when the agent reports to human management."""
        p = self.get_profile(agent_id)
        return None if p.reports_to is None else self.get_profile(p.reports_to)

    def get_direct_reports(self, agent_id: str) -> list[AgentProfile]:
        self.get_profile(agent_id)
        return [p for p in self.list_agents(include_disabled=True) if p.reports_to == agent_id]

    def validation_report(self) -> dict[str, list[str]]:
        report = {aid: list(errs) for aid, errs in self._invalid.items()}
        for aid in self._missing:
            report[aid] = ["profile file missing"]
        return report

    def assert_all_valid(self) -> None:
        report = self.validation_report()
        if report:
            raise InvalidAgentProfileError(
                "agent registry has invalid profiles: "
                + " | ".join(f"{k}: {'; '.join(v)}" for k, v in sorted(report.items())))
