"""Canonical tool registry (Part 4).

This is the only place tool names are defined. An agent profile may list a tool only if
the tool exists here AND the agent's role is in the tool's `allowed_roles`. A tool with an
empty `allowed_roles` is defined for classification and audit, but no agent can use it.

`requires_approval` is the tool's floor; the risk engine can only raise it, never lower it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .common import AgentRole as Role
from .common import RiskClass, StrEnum
from .errors import UnknownToolError


class SideEffect(StrEnum):
    NONE = "NONE"                                   # read-only
    INTERNAL_WRITE = "INTERNAL_WRITE"               # Command Center records only
    EXTERNAL_COMMUNICATION = "EXTERNAL_COMMUNICATION"
    EXTERNAL_RECORD_CHANGE = "EXTERNAL_RECORD_CHANGE"
    FINANCIAL_COMMITMENT = "FINANCIAL_COMMITMENT"
    FINANCIAL_TRANSFER = "FINANCIAL_TRANSFER"
    LEGAL_COMMITMENT = "LEGAL_COMMITMENT"
    EMPLOYMENT = "EMPLOYMENT"
    ACCESS_CONTROL = "ACCESS_CONTROL"
    DESTRUCTIVE = "DESTRUCTIVE"


class ToolCategory(StrEnum):
    READ = "READ"
    CONTROLLED_WRITE = "CONTROLLED_WRITE"
    EXTERNAL = "EXTERNAL"
    HIGH_RISK = "HIGH_RISK"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    category: ToolCategory
    default_risk_class: RiskClass
    reversible: bool
    requires_approval: bool
    allowed_roles: frozenset[Role]
    side_effect_type: SideEffect

    def __post_init__(self) -> None:
        if self.default_risk_class is RiskClass.RED and not self.requires_approval:
            raise ValueError(f"{self.name}: RED tools must require approval")
        if self.default_risk_class is RiskClass.AMBER and not self.requires_approval:
            raise ValueError(f"{self.name}: AMBER tools must require approval by default")
        if self.side_effect_type is SideEffect.NONE and self.category is not ToolCategory.READ:
            raise ValueError(f"{self.name}: only READ tools may have no side effect")


ALL = frozenset(Role)


def _roles(*roles: Role) -> frozenset[Role]:
    return frozenset(roles)


def _read(name: str, description: str, roles: frozenset[Role]) -> ToolSpec:
    return ToolSpec(name, description, ToolCategory.READ, RiskClass.GREEN, True, False, roles, SideEffect.NONE)


def _write(name: str, description: str, roles: frozenset[Role]) -> ToolSpec:
    return ToolSpec(name, description, ToolCategory.CONTROLLED_WRITE, RiskClass.GREEN, True, False, roles,
                    SideEffect.INTERNAL_WRITE)


_TOOLS: tuple[ToolSpec, ...] = (
    # ---- read tools ----------------------------------------------------------------
    _read("read_kpis", "Read KPI definitions and recorded observations.", ALL),
    _read("read_tasks", "Read tasks and their status.", ALL),
    _read("read_projects", "Read projects, milestones, capacity and delivery status.",
          _roles(Role.CHIEF_OF_STAFF, Role.CFO, Role.OPERATIONS, Role.CUSTOMER_SUCCESS, Role.PRODUCT_ANALYST,
                 Role.PROCUREMENT, Role.DATA_ANALYST, Role.LEGAL_RISK, Role.PEOPLE_PERFORMANCE)),
    _read("read_customers", "Read customer accounts, health, usage and support history.",
          _roles(Role.CHIEF_OF_STAFF, Role.SALES_DIRECTOR, Role.LEAD_GENERATION, Role.MARKETING_DIRECTOR, Role.CFO,
                 Role.OPERATIONS, Role.CUSTOMER_SUCCESS, Role.PRODUCT_ANALYST, Role.DATA_ANALYST, Role.LEGAL_RISK)),
    _read("read_leads", "Read leads, enrichment data and lead scores.",
          _roles(Role.CHIEF_OF_STAFF, Role.SALES_DIRECTOR, Role.LEAD_GENERATION, Role.MARKETING_DIRECTOR,
                 Role.DATA_ANALYST)),
    _read("read_opportunities", "Read pipeline opportunities, stages, values and margins.",
          _roles(Role.CHIEF_OF_STAFF, Role.SALES_DIRECTOR, Role.LEAD_GENERATION, Role.MARKETING_DIRECTOR, Role.CFO,
                 Role.CUSTOMER_SUCCESS, Role.PRODUCT_ANALYST, Role.DATA_ANALYST)),
    _read("read_financial_summary", "Read cash, AR/AP, margin, budget and forecast summaries.",
          _roles(Role.CHIEF_OF_STAFF, Role.SALES_DIRECTOR, Role.MARKETING_DIRECTOR, Role.CFO, Role.OPERATIONS,
                 Role.PRODUCT_ANALYST, Role.PROCUREMENT, Role.DATA_ANALYST)),
    _read("read_vendors", "Read vendors, quotations, contracts metadata and supplier performance.",
          _roles(Role.CHIEF_OF_STAFF, Role.CFO, Role.OPERATIONS, Role.PROCUREMENT, Role.DATA_ANALYST,
                 Role.LEGAL_RISK)),
    _read("read_knowledge", "Read the approved company knowledge base within the agent's knowledge scopes.", ALL),
    _read("search_company_data", "Search authorized company data within the agent's knowledge scopes.", ALL),

    # ---- controlled internal writes ------------------------------------------------
    _write("create_task", "Create an internal task in the Command Center.", ALL),
    _write("update_task_status", "Move an internal task through its state machine.", ALL),
    _write("create_output", "Persist a structured agent output.", ALL),
    _write("create_recommendation", "Record a recommendation for human review.", ALL),
    _write("request_approval", "Open an approval request for a proposed action.", ALL),
    _write("create_alert", "Raise an internal alert.", ALL),
    _write("record_kpi_observation", "Record a KPI observation with its source (never redefines a KPI).",
           _roles(Role.CFO, Role.DATA_ANALYST)),
    _write("propose_change_request", "Draft a change request from an approved recommendation.", ALL),

    # ---- external effects (AMBER floor) -------------------------------------------------
    ToolSpec("send_customer_message", "Send a message to a customer or prospect.", ToolCategory.EXTERNAL,
             RiskClass.AMBER, False, True,
             _roles(Role.SALES_DIRECTOR, Role.LEAD_GENERATION, Role.CUSTOMER_SUCCESS),
             SideEffect.EXTERNAL_COMMUNICATION),
    ToolSpec("publish_marketing_content", "Publish marketing content to an external channel.",
             ToolCategory.EXTERNAL, RiskClass.AMBER, False, True, _roles(Role.MARKETING_DIRECTOR),
             SideEffect.EXTERNAL_COMMUNICATION),
    ToolSpec("create_purchase_order", "Create a purchase order with a supplier.", ToolCategory.EXTERNAL,
             RiskClass.AMBER, False, True, _roles(Role.PROCUREMENT), SideEffect.FINANCIAL_COMMITMENT),
    ToolSpec("modify_customer_record", "Change a customer record in the system of record.", ToolCategory.EXTERNAL,
             RiskClass.AMBER, True, True, _roles(Role.CUSTOMER_SUCCESS), SideEffect.EXTERNAL_RECORD_CHANGE),
    ToolSpec("schedule_meeting", "Schedule a meeting with external participants.", ToolCategory.EXTERNAL,
             RiskClass.AMBER, True, True,
             _roles(Role.SALES_DIRECTOR, Role.LEAD_GENERATION, Role.CUSTOMER_SUCCESS),
             SideEffect.EXTERNAL_COMMUNICATION),

    # ---- high risk (RED floor) --------------------------------------------------------------
    # CFO is the only role that may ever be granted initiate_payment, and no shipped profile
    # grants it. The other high-risk tools cannot be granted to any agent.
    ToolSpec("initiate_payment", "Initiate an outgoing payment.", ToolCategory.HIGH_RISK, RiskClass.RED, False,
             True, _roles(Role.CFO), SideEffect.FINANCIAL_TRANSFER),
    ToolSpec("sign_contract", "Execute a contract.", ToolCategory.HIGH_RISK, RiskClass.RED, False, True,
             frozenset(), SideEffect.LEGAL_COMMITMENT),
    ToolSpec("approve_loan", "Approve or draw a loan.", ToolCategory.HIGH_RISK, RiskClass.RED, False, True,
             frozenset(), SideEffect.FINANCIAL_COMMITMENT),
    ToolSpec("terminate_employee", "Terminate an employee.", ToolCategory.HIGH_RISK, RiskClass.RED, False, True,
             frozenset(), SideEffect.EMPLOYMENT),
    ToolSpec("modify_permissions", "Change user or agent permissions.", ToolCategory.HIGH_RISK, RiskClass.RED,
             True, True, frozenset(), SideEffect.ACCESS_CONTROL),
    ToolSpec("irreversible_delete", "Permanently delete data.", ToolCategory.HIGH_RISK, RiskClass.RED, False, True,
             frozenset(), SideEffect.DESTRUCTIVE),
)


class ToolRegistry:
    def __init__(self, tools: tuple[ToolSpec, ...] = _TOOLS):
        names = [t.name for t in tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool names in registry")
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"unknown tool {name!r}") from None

    def exists(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    def role_may_use(self, role: Role, name: str) -> bool:
        return role in self.get(name).allowed_roles


DEFAULT_TOOL_REGISTRY = ToolRegistry()
