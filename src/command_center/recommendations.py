"""Recommendation lifecycle (Part 11), change requests (Part 12) and the business outcome
loop (Part 13).

- Only humans approve, reject or verify recommendations, and approve, close or roll back
  change requests.
- §27: a rejected recommendation cannot be re-proposed unless it brings new evidence.
- Outcomes: expected values are stored at change creation. Actual values can only be
  recorded with source references, after the measurement window has ended. Nothing is
  estimated or invented.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .agents import KpiDirection
from .audit import AuditLogger, EventType
from .common import ActorType, Clock, Confidence, Level, Principal, StrEnum, new_id, utc_now
from .errors import PermissionDeniedError, PolicyViolationError, ValidationFailedError
from .sources import SourceCatalog
from .state_machines import (
    CHANGE_MACHINE,
    HUMAN_ONLY_CHANGE_TARGETS,
    HUMAN_ONLY_RECOMMENDATION_TARGETS,
    RECOMMENDATION_MACHINE,
    ChangeStatus,
    RecommendationStatus,
)
from .storage import InMemoryRepository, Repository


class ImpactCategory(StrEnum):
    REVENUE = "REVENUE"
    GROSS_MARGIN = "GROSS_MARGIN"
    CASHFLOW = "CASHFLOW"
    CUSTOMER_VALUE = "CUSTOMER_VALUE"
    OPERATIONAL_RELIABILITY = "OPERATIONAL_RELIABILITY"
    PRODUCTIVITY = "PRODUCTIVITY"
    RISK_REDUCTION = "RISK_REDUCTION"
    STRATEGIC = "STRATEGIC"


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str
    basis: str
    period: str | None = None

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any] | None) -> "Money | None":
        if raw is None:
            return None
        return cls(Decimal(str(raw["amount"])), raw["currency"], raw["basis"], raw.get("period"))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def recommendation_fingerprint(problem: str, recommendation: str) -> str:
    return hashlib.sha256(f"{_norm(problem)}|{_norm(recommendation)}".encode()).hexdigest()


@dataclass(frozen=True)
class Recommendation:
    recommendation_id: str
    organization_id: str
    source_agent: str
    run_id: str | None
    problem: str
    evidence: tuple[str, ...]
    evidence_source_ids: tuple[str, ...]
    recommendation: str
    why_now: str
    expected_impact: str
    impact_category: ImpactCategory
    estimated_value: Money | None
    estimated_cost: Money | None
    effort: Level
    urgency: Level
    dependencies: tuple[str, ...]
    risks: tuple[str, ...]
    reversible: bool
    confidence: Confidence
    suggested_owner: str
    kpis_affected: tuple[str, ...]
    created_at: datetime
    fingerprint: str
    status: RecommendationStatus = RecommendationStatus.PROPOSED
    decided_by: str | None = None
    decision_reason: str | None = None
    supersedes: str | None = None
    version: int = 0


class OutcomeStatus(StrEnum):
    NOT_MEASURED = "NOT_MEASURED"
    PARTIAL = "PARTIAL"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class OutcomeMeasurement:
    kpi: str
    unit: str
    direction: KpiDirection
    baseline: Decimal
    target: Decimal
    expected_value: Decimal              # predicted value of the KPI at measurement_end
    measurement_start: datetime
    measurement_end: datetime
    actual: Decimal | None = None
    actual_source_ids: tuple[str, ...] = ()
    measured_at: datetime | None = None
    measured_by: str | None = None
    outcome_status: OutcomeStatus = OutcomeStatus.NOT_MEASURED
    note: str | None = None

    @property
    def variance(self) -> Decimal | None:
        """actual - target (None until measured)."""
        return None if self.actual is None else self.actual - self.target

    @property
    def variance_vs_expected(self) -> Decimal | None:
        return None if self.actual is None else self.actual - self.expected_value

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "OutcomeMeasurement":
        if raw.get("actual") is not None:
            raise ValidationFailedError("actual values cannot be supplied at creation; record them after measurement")
        start, end = datetime.fromisoformat(str(raw["measurement_start"])), datetime.fromisoformat(str(raw["measurement_end"]))
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValidationFailedError("measurement window must be timezone-aware with end after start")
        direction = KpiDirection(raw["direction"])
        if direction is KpiDirection.TARGET_RANGE:
            raise ValidationFailedError("TARGET_RANGE outcomes are not supported yet; state a directional target")
        return cls(kpi=raw["kpi"], unit=raw["unit"], direction=direction,
                   baseline=Decimal(str(raw["baseline"])), target=Decimal(str(raw["target"])),
                   expected_value=Decimal(str(raw["expected_value"])), measurement_start=start, measurement_end=end)


def evaluate_outcome(m: OutcomeMeasurement) -> OutcomeStatus:
    if m.actual is None:
        return OutcomeStatus.NOT_MEASURED
    sign = 1 if m.direction is KpiDirection.HIGHER_IS_BETTER else -1
    progress = (m.actual - m.baseline) * sign
    if (m.actual - m.target) * sign >= 0:
        return OutcomeStatus.SUCCESS
    if progress > 0:
        return OutcomeStatus.PARTIAL
    return OutcomeStatus.FAILED


@dataclass(frozen=True)
class ChangeRequest:
    change_id: str
    organization_id: str
    recommendation_id: str
    current_state: str
    problem: str
    desired_state: str
    change: str
    expected_benefit: str
    estimated_cost: Money | None
    risk: str
    implementation_steps: tuple[str, ...]
    validation_method: str
    rollback_plan: str
    expected_kpi_change: tuple[OutcomeMeasurement, ...]
    owner: str
    created_by: str
    created_at: datetime
    status: ChangeStatus = ChangeStatus.DRAFT
    rollback_reason: str | None = None
    version: int = 0

    @property
    def actual_kpi_change(self) -> tuple[OutcomeMeasurement, ...]:
        return tuple(m for m in self.expected_kpi_change if m.actual is not None)


@dataclass
class RecommendationService:
    audit: AuditLogger
    repo: Repository[Recommendation] = field(default_factory=lambda: InMemoryRepository("recommendation_id"))
    changes: Repository[ChangeRequest] = field(default_factory=lambda: InMemoryRepository("change_id"))
    clock: Clock = utc_now

    # ---- recommendations ---------------------------------------------------------------
    def create(self, data: Mapping[str, Any], *, organization_id: str, source_agent: str, run_id: str | None,
               sources: SourceCatalog, supersedes: str | None = None) -> Recommendation:
        """`data` follows the schema's recommendation shape (§7)."""
        evidence_ids = tuple(data.get("evidence_source_ids", ()))
        missing = sources.missing(evidence_ids)
        if missing:
            raise ValidationFailedError("recommendation cites unknown sources", missing)
        if not data.get("evidence"):
            raise ValidationFailedError("a recommendation must include evidence")
        fp = recommendation_fingerprint(data["problem_or_opportunity"], data["recommendation"])

        # §27: do not re-propose a rejected recommendation without materially new evidence.
        for prior in self.repo.list(lambda r: r.fingerprint == fp and r.organization_id == organization_id
                                    and r.status is RecommendationStatus.REJECTED):
            new_evidence = set(data["evidence"]) - set(prior.evidence)
            new_sources = set(evidence_ids) - set(prior.evidence_source_ids)
            if not (new_evidence or new_sources):
                raise PolicyViolationError(
                    f"recommendation matches rejected {prior.recommendation_id} with no new evidence")
        rec = Recommendation(
            recommendation_id=new_id("rec"), organization_id=organization_id, source_agent=source_agent,
            run_id=run_id, problem=data["problem_or_opportunity"], evidence=tuple(data["evidence"]),
            evidence_source_ids=evidence_ids, recommendation=data["recommendation"], why_now=data["why_now"],
            expected_impact=data["expected_impact"], impact_category=ImpactCategory(data["impact_category"]),
            estimated_value=Money.from_wire(data.get("estimated_value")),
            estimated_cost=Money.from_wire(data.get("estimated_cost")), effort=Level(data["effort"]),
            urgency=Level(data["urgency"]), dependencies=tuple(data.get("dependencies", ())),
            risks=tuple(data.get("risks", ())), reversible=bool(data["reversible"]),
            confidence=Confidence(data["confidence"]), suggested_owner=data["suggested_owner"],
            kpis_affected=tuple(data.get("kpis_affected", ())), created_at=self.clock(), fingerprint=fp,
            supersedes=supersedes)
        self.repo.add(rec)
        self.audit.record(EventType.RECOMMENDATION_CREATED, organization_id=organization_id,
                          actor_type=ActorType.AGENT, actor_id=source_agent, agent_id=source_agent, run_id=run_id,
                          object_type="recommendation", object_id=rec.recommendation_id,
                          payload={"problem": rec.problem, "impact_category": rec.impact_category.value,
                                   "confidence": rec.confidence.value}, new_state=rec.status)
        return rec

    def transition(self, recommendation_id: str, target: RecommendationStatus, *, actor_type: ActorType,
                   actor_id: str, principal: Principal | None = None, reason: str | None = None) -> Recommendation:
        rec = self.repo.get(recommendation_id)
        RECOMMENDATION_MACHINE.check(rec.status, target)
        if target in HUMAN_ONLY_RECOMMENDATION_TARGETS:
            if principal is None or not principal.is_human or actor_type is not ActorType.HUMAN \
                    or principal.principal_id != actor_id:
                raise PermissionDeniedError(f"only a human may move a recommendation to {target.value}")
            if principal.organization_id != rec.organization_id:
                raise PermissionDeniedError("different organization")
        if target in (RecommendationStatus.REJECTED, RecommendationStatus.ROLLED_BACK) and not (reason or "").strip():
            raise ValidationFailedError(f"{target.value} requires a reason")
        if target is RecommendationStatus.VERIFIED:
            linked = self.changes.list(lambda c: c.recommendation_id == recommendation_id)
            measured = [m for c in linked for m in c.expected_kpi_change if m.outcome_status is not OutcomeStatus.NOT_MEASURED]
            if not measured:
                raise ValidationFailedError("VERIFIED requires at least one measured outcome")
        changes: dict[str, Any] = {"status": target}
        if target in HUMAN_ONLY_RECOMMENDATION_TARGETS:
            changes.update(decided_by=actor_id, decision_reason=reason)
        updated = self.repo.update(replace(rec, **changes), expected_version=rec.version)
        event = {RecommendationStatus.APPROVED: EventType.RECOMMENDATION_APPROVED,
                 RecommendationStatus.REJECTED: EventType.RECOMMENDATION_REJECTED}.get(
            target, EventType.RECOMMENDATION_UPDATED)
        self.audit.record(event, organization_id=rec.organization_id, actor_type=actor_type, actor_id=actor_id,
                          agent_id=rec.source_agent, run_id=rec.run_id, object_type="recommendation",
                          object_id=recommendation_id, payload={"reason": reason} if reason else None,
                          previous_state=rec.status, new_state=target)
        return updated

    # ---- change requests ---------------------------------------------------------------
    def create_change_request(self, recommendation_id: str, data: Mapping[str, Any], *, created_by: str,
                              actor_type: ActorType) -> ChangeRequest:
        rec = self.repo.get(recommendation_id)
        if rec.status not in (RecommendationStatus.APPROVED, RecommendationStatus.IMPLEMENTATION):
            raise PolicyViolationError("change requests can only be created from an APPROVED recommendation")
        errors = [f"{k} is required" for k in ("current_state", "problem", "desired_state", "change",
                                                  "expected_benefit", "risk", "validation_method",
                                                  "rollback_plan", "owner")
                  if not str(data.get(k, "")).strip()]
        if not data.get("implementation_steps"):
            errors.append("implementation_steps must not be empty")
        if errors:
            raise ValidationFailedError("invalid change request", errors)
        outcomes = tuple(OutcomeMeasurement.from_wire(m) for m in data.get("expected_kpi_change", ()))
        cr = ChangeRequest(change_id=new_id("chg"), organization_id=rec.organization_id,
                           recommendation_id=recommendation_id, current_state=data["current_state"],
                           problem=data["problem"], desired_state=data["desired_state"], change=data["change"],
                           expected_benefit=data["expected_benefit"],
                           estimated_cost=Money.from_wire(data.get("estimated_cost")), risk=data["risk"],
                           implementation_steps=tuple(data["implementation_steps"]),
                           validation_method=data["validation_method"], rollback_plan=data["rollback_plan"],
                           expected_kpi_change=outcomes, owner=data["owner"], created_by=created_by,
                           created_at=self.clock())
        self.changes.add(cr)
        self.audit.record(EventType.CHANGE_REQUEST_CREATED, organization_id=cr.organization_id,
                          actor_type=actor_type, actor_id=created_by, agent_id=rec.source_agent, run_id=rec.run_id,
                          object_type="change_request", object_id=cr.change_id,
                          payload={"recommendation_id": recommendation_id,
                                   "expected_kpis": [m.kpi for m in outcomes]}, new_state=cr.status)
        return cr

    def transition_change(self, change_id: str, target: ChangeStatus, *, actor_type: ActorType, actor_id: str,
                          principal: Principal | None = None, reason: str | None = None) -> ChangeRequest:
        cr = self.changes.get(change_id)
        CHANGE_MACHINE.check(cr.status, target)
        if target in HUMAN_ONLY_CHANGE_TARGETS:
            if principal is None or not principal.is_human or actor_type is not ActorType.HUMAN \
                    or principal.principal_id != actor_id:
                raise PermissionDeniedError(f"only a human may move a change request to {target.value}")
        if target is ChangeStatus.ROLLED_BACK and not (reason or "").strip():
            raise ValidationFailedError("rollback requires a reason")
        if target is ChangeStatus.CLOSED and any(m.outcome_status is OutcomeStatus.NOT_MEASURED
                                                 for m in cr.expected_kpi_change):
            raise ValidationFailedError("cannot close a change with unmeasured expected outcomes")
        updated = self.changes.update(replace(cr, status=target,
                                              rollback_reason=reason if target is ChangeStatus.ROLLED_BACK else None),
                                      expected_version=cr.version)
        self.audit.record(EventType.CHANGE_REQUEST_UPDATED, organization_id=cr.organization_id,
                          actor_type=actor_type, actor_id=actor_id, object_type="change_request", object_id=change_id,
                          payload={"reason": reason} if reason else None, previous_state=cr.status, new_state=target)
        return updated

    def record_outcome(self, change_id: str, kpi: str, *, actual: Decimal | int | str, sources: SourceCatalog,
                       source_ids: list[str], measured_by: Principal, inconclusive: bool = False,
                       note: str | None = None) -> ChangeRequest:
        """Record a measured actual. Needs sources; refuses before the window ends."""
        if not measured_by.is_human:
            raise PermissionDeniedError("outcomes are recorded by a human (or a human-approved data pipeline)")
        sources.require(source_ids, f"outcome {kpi}")
        if inconclusive and not (note or "").strip():
            raise ValidationFailedError("an INCONCLUSIVE outcome needs a note explaining why")
        cr = self.changes.get(change_id)
        if cr.status not in (ChangeStatus.COMPLETED, ChangeStatus.IMPACT_MEASUREMENT):
            raise PolicyViolationError("outcomes are measured after the change is COMPLETED")
        now = self.clock()
        idx = next((i for i, m in enumerate(cr.expected_kpi_change) if m.kpi == kpi), None)
        if idx is None:
            raise ValidationFailedError(f"no expected outcome for KPI {kpi!r}")
        m = cr.expected_kpi_change[idx]
        if now < m.measurement_end:
            raise PolicyViolationError("measurement window has not ended; actual values cannot be recorded yet")
        measured = replace(m, actual=Decimal(str(actual)), actual_source_ids=tuple(source_ids), measured_at=now,
                           measured_by=measured_by.principal_id, note=note)
        measured = replace(measured, outcome_status=OutcomeStatus.INCONCLUSIVE if inconclusive
                           else evaluate_outcome(measured))
        outcomes = list(cr.expected_kpi_change)
        outcomes[idx] = measured
        updated = self.changes.update(replace(cr, expected_kpi_change=tuple(outcomes)), expected_version=cr.version)
        rec = self.repo.get(cr.recommendation_id)
        self.audit.record(EventType.OUTCOME_RECORDED, organization_id=cr.organization_id, actor_type=ActorType.HUMAN,
                          actor_id=measured_by.principal_id, agent_id=rec.source_agent, run_id=rec.run_id,
                          object_type="change_request", object_id=change_id,
                          payload={"kpi": kpi, "baseline": str(m.baseline), "target": str(m.target),
                                   "expected_value": str(m.expected_value), "actual": str(measured.actual),
                                   "variance": str(measured.variance), "outcome_status": measured.outcome_status.value,
                                   "source_ids": source_ids})
        return updated
