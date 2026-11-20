"""Multi-tenancy (Part 26).

Model: one CommandCenter (composition root) per organization. Every repository a tenant uses
is scoped to that organization (in memory: separate instances; PostgreSQL: every query
filters on organization_id and RLS enforces it again). Agent ids are ORGANIZATION-SCOPED:
`cfo` in org A and `cfo` in org B are different rows, keyed (organization_id, agent_key).

A principal reaches only the tenant of its own organization_id. Records from another
organization are reported as not found (no existence leak).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .common import Principal
from .errors import NotFoundError, PermissionDeniedError


@dataclass
class TenantRegistry:
    tenants: dict[str, Any] = field(default_factory=dict)   # organization_id -> CommandCenter

    def register(self, cc: Any) -> None:
        org = cc.policy.organization_id
        if org in self.tenants:
            raise ValueError(f"organization {org!r} already registered")
        self.tenants[org] = cc

    def for_principal(self, principal: Principal) -> Any:
        if not principal.is_human:
            raise PermissionDeniedError("API access requires an authenticated human principal")
        cc = self.tenants.get(principal.organization_id)
        if cc is None:
            raise PermissionDeniedError("principal's organization is not served here")
        return cc


def owned(record: Any, principal: Principal, what: str = "record") -> Any:
    """Return the record only if it belongs to the principal's organization."""
    if record is None or getattr(record, "organization_id", None) != principal.organization_id:
        raise NotFoundError(f"{what} not found")
    return record
