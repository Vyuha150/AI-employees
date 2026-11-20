"""Shared enums, identities and helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    """A random UUID (string form), usable as a PostgreSQL uuid primary key.

    `prefix` is accepted for call-site readability only; it is not part of the id, because
    database ids are UUIDs (Part 25). Type information lives in the column/table, not the id."""
    return str(uuid.uuid4())


def require_aware(dt: datetime, label: str = "datetime") -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return dt


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class RiskClass(StrEnum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"

    @property
    def rank(self) -> int:
        return {"GREEN": 0, "AMBER": 1, "RED": 2}[self.value]

    @staticmethod
    def max(*classes: "RiskClass") -> "RiskClass":
        return max(classes, key=lambda c: c.rank)


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Level(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ActorType(StrEnum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True)
class Principal:
    """A human (or system) identity as authenticated by the application.

    `roles` decide which approvals the principal may give (see OrganizationPolicy.approver_roles).
    `permissions` are explicit capability strings. There are no wildcards and no implied permissions.

    Known permission strings:
      run_agent:<agent_id>   may start a run of that agent
      auto_execute:GREEN     GREEN actions in runs this principal triggers may execute without approval
      auto_execute:AMBER     additionally required for organization-listed AMBER auto-execution
    """

    principal_id: str
    organization_id: str
    actor_type: ActorType = ActorType.HUMAN
    roles: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def is_human(self) -> bool:
        return self.actor_type is ActorType.HUMAN


class AgentRole(StrEnum):
    CHIEF_OF_STAFF = "CHIEF_OF_STAFF"
    SALES_DIRECTOR = "SALES_DIRECTOR"
    LEAD_GENERATION = "LEAD_GENERATION"
    MARKETING_DIRECTOR = "MARKETING_DIRECTOR"
    CFO = "CFO"
    OPERATIONS = "OPERATIONS"
    CUSTOMER_SUCCESS = "CUSTOMER_SUCCESS"
    PRODUCT_ANALYST = "PRODUCT_ANALYST"
    PROCUREMENT = "PROCUREMENT"
    DATA_ANALYST = "DATA_ANALYST"
    LEGAL_RISK = "LEGAL_RISK"
    PEOPLE_PERFORMANCE = "PEOPLE_PERFORMANCE"
