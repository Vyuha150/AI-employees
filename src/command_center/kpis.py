"""KPI snapshots: sourced observations of defined KPIs. Values are never computed or guessed
here; every snapshot carries its source reference."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .audit import AuditLogger, EventType
from .common import ActorType, Clock, new_id, utc_now
from .errors import ValidationFailedError
from .storage import InMemoryRepository, Repository


@dataclass(frozen=True)
class KpiSnapshot:
    snapshot_id: str
    organization_id: str
    kpi: str
    value: Decimal
    unit: str
    observed_at: str
    source_reference: str
    recorded_by: str
    run_id: str | None
    recorded_at: datetime
    version: int = 0


@dataclass
class KpiService:
    audit: AuditLogger
    repo: Repository[KpiSnapshot] = field(default_factory=lambda: InMemoryRepository("snapshot_id"))
    clock: Clock = utc_now

    def record(self, data: Mapping[str, Any], *, organization_id: str, recorded_by: str,
               run_id: str | None = None) -> KpiSnapshot:
        missing = [k for k in ("kpi", "value", "unit", "observed_at", "source_reference") if not data.get(k)]
        if missing:
            raise ValidationFailedError("KPI observation incomplete (a source is mandatory)", missing)
        try:
            value = Decimal(str(data["value"]))
        except (InvalidOperation, ValueError):
            raise ValidationFailedError("KPI value must be numeric") from None
        snap = KpiSnapshot(new_id("kpi"), organization_id, data["kpi"], value, data["unit"], str(data["observed_at"]),
                           data["source_reference"], recorded_by, run_id, self.clock())
        self.repo.add(snap)
        self.audit.record(EventType.KPI_OBSERVATION_RECORDED, organization_id=organization_id,
                          actor_type=ActorType.AGENT, actor_id=recorded_by, agent_id=recorded_by, run_id=run_id,
                          object_type="kpi_snapshot", object_id=snap.snapshot_id,
                          payload={"kpi": snap.kpi, "value": str(value), "unit": snap.unit,
                                   "source_reference": snap.source_reference})
        return snap

    def history(self, organization_id: str, kpi: str, limit: int = 100) -> list[KpiSnapshot]:
        rows = self.repo.list(lambda s: s.organization_id == organization_id and s.kpi == kpi)
        return sorted(rows, key=lambda s: (s.observed_at, s.recorded_at), reverse=True)[:limit]

    def latest(self, organization_id: str, kpi: str) -> KpiSnapshot | None:
        h = self.history(organization_id, kpi, 1)
        return h[0] if h else None

    def list_kpis(self, organization_id: str) -> list[str]:
        return sorted({s.kpi for s in self.repo.list(lambda s: s.organization_id == organization_id)})
