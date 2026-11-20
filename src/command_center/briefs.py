"""Daily brief (Part 19, §17) and weekly review (Part 20) contracts.

Schema validation plus rules a schema cannot express:
- at most 7 priorities (schema), a warning above the preferred 5;
- every cited source_id must resolve to a listed source;
- a priority's source_agent must be a registered agent;
- a verified root cause must cite evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

from .errors import ValidationFailedError
from .resources import load_json_schema
from .validate import ValidationResult

_DAILY = Draft202012Validator(load_json_schema("daily_brief.schema.json"))
_WEEKLY = Draft202012Validator(load_json_schema("weekly_review.schema.json"))
_WEEKLY_V1 = Draft202012Validator(load_json_schema("weekly_review_v1.schema.json"))  # legacy draft shape

MAX_PRIORITIES = 7
PREFERRED_PRIORITIES = 5
MAX_WEEKLY_RECOMMENDATIONS = 5
WEEKLY_DOMAINS = ("revenue", "profit", "cash", "pipeline", "delivery", "customers", "costs", "people", "risk")


def _schema_errors(validator: Draft202012Validator, doc: Mapping[str, Any]) -> list[str]:
    return [f"schema: {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.path))]


def _walk_source_refs(node: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(node, Mapping):
        for k, v in node.items():
            if k in ("source_ids", "evidence_source_ids") and isinstance(v, list):
                for ref in v:
                    yield f"{path}/{k}", ref
            elif k != "sources":
                yield from _walk_source_refs(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_source_refs(v, f"{path}/{i}")


def _check_refs(doc: Mapping[str, Any], result: ValidationResult) -> None:
    known = {s["id"] for s in doc.get("sources", [])}
    for where, ref in _walk_source_refs(doc):
        if ref not in known:
            result.errors.append(f"{where}: source_id {ref!r} is not listed in sources")


def validate_daily_brief(doc: Mapping[str, Any], known_agents: Iterable[str] | None = None,
                         *, require_data_quality: bool = False) -> ValidationResult:
    result = ValidationResult(errors=_schema_errors(_DAILY, doc))
    if result.errors:
        return result
    if require_data_quality and "data_quality" not in doc:
        result.errors.append("data_quality is required for generated briefs")
    dq = doc.get("data_quality")
    if dq and dq["overall_status"] != "GOOD":
        result.warnings.append(f"data quality is {dq['overall_status']}: brief is not authoritative")
    _check_refs(doc, result)
    n = len(doc["todays_priorities"])
    if n > PREFERRED_PRIORITIES:
        result.warnings.append(f"{n} priorities; prefer {PREFERRED_PRIORITIES} or fewer (max {MAX_PRIORITIES})")
    if known_agents is not None:
        agents = set(known_agents)
        for i, p in enumerate(doc["todays_priorities"]):
            if p["source_agent"] not in agents:
                result.errors.append(f"todays_priorities/{i}: unknown source_agent {p['source_agent']!r}")
    for i, d in enumerate(doc["decisions_required"]):
        if not d["options"]:
            result.warnings.append(f"decisions_required/{i}: no options given for a management decision")
    return result


def validate_weekly_review(doc: Mapping[str, Any], *, max_recommendations: int = MAX_WEEKLY_RECOMMENDATIONS,
                           known_agents: Iterable[str] | None = None) -> ValidationResult:
    """Validate a weekly review. Documents in the legacy draft shape (week_start/week_end)
    are validated against the v1 schema; everything else against the v2 contract."""
    if "week_start" in doc:
        return _validate_weekly_v1(doc)
    result = ValidationResult(errors=_schema_errors(_WEEKLY, doc))
    if result.errors:
        return result
    _check_refs(doc, result)
    if doc["review_period_end"] < doc["review_period_start"]:
        result.errors.append("review_period_end is before review_period_start")
    if len(doc["recommendations"]) > max_recommendations:
        result.errors.append(f"{len(doc['recommendations'])} recommendations; maximum is {max_recommendations}")
    n = len(doc["priorities_next_week"])
    if n > PREFERRED_PRIORITIES:
        result.warnings.append(f"{n} priorities next week; prefer {PREFERRED_PRIORITIES} or fewer (max {MAX_PRIORITIES})")
    for i, rc in enumerate(doc["root_causes"]):
        if rc["verified"] and not rc["source_ids"]:
            result.errors.append(f"root_causes/{i}: a verified root cause must cite evidence")
    for i, f in enumerate(doc["top_failures"]):
        if f["root_cause_status"] == "VERIFIED" and not f["source_ids"]:
            result.errors.append(f"top_failures/{i}: VERIFIED root cause status needs source_ids")
    if known_agents is not None and doc["generated_by_agent"] not in set(known_agents):
        result.errors.append(f"generated_by_agent {doc['generated_by_agent']!r} is not a registered agent")
    problems = data_quality_problems(doc)
    if doc["authoritative"] and problems:
        result.errors.append("review is marked authoritative but " + "; ".join(problems))
    elif problems:
        result.warnings.append("review is not authoritative: " + "; ".join(problems))
    return result


def data_quality_problems(doc: Mapping[str, Any]) -> list[str]:
    dq = doc["data_quality"]
    problems = []
    if dq["overall_status"] != "GOOD":
        problems.append(f"data quality is {dq['overall_status']}")
    if dq["stale_sources"]:
        problems.append(f"{len(dq['stale_sources'])} stale source(s)")
    if dq["missing_sources"]:
        problems.append(f"{len(dq['missing_sources'])} missing source(s)")
    if dq["conflicting_sources"]:
        problems.append(f"{len(dq['conflicting_sources'])} conflicting source(s)")
    weak = [k for k in WEEKLY_DOMAINS if doc[k]["status"] in ("UNAVAILABLE", "PARTIAL")]
    if weak:
        problems.append("domains unavailable or partial: " + ", ".join(weak))
    return problems


def _validate_weekly_v1(doc: Mapping[str, Any]) -> ValidationResult:
    result = ValidationResult(errors=_schema_errors(_WEEKLY_V1, doc))
    if result.errors:
        return result
    _check_refs(doc, result)
    if doc["week_end"] < doc["week_start"]:
        result.errors.append("week_end is before week_start")
    for i, rc in enumerate(doc["root_causes"]):
        if rc["status"] == "VERIFIED" and not rc.get("evidence_source_ids"):
            result.errors.append(f"root_causes/{i}: a VERIFIED root cause must cite evidence")
    return result


# ---------------------------------------------------------------------------------------
# typed weekly review (Part 20)
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DomainSection:
    status: str
    summary: str
    trend: str
    key_findings: tuple[str, ...]
    risks: tuple[str, ...]
    recommended_actions: tuple[str, ...]
    source_ids: tuple[str, ...]
    current_value: Any = None
    target: Any = None
    variance: Any = None
    unit: str | None = None

    @classmethod
    def unavailable(cls, reason: str) -> "DomainSection":
        return cls("UNAVAILABLE", reason, "UNKNOWN", (), (), (), ())


@dataclass(frozen=True)
class WinItem:
    title: str
    business_impact: str
    evidence: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class FailureItem:
    title: str
    business_impact: str
    root_cause_status: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class RootCause:
    problem: str
    cause: str
    confidence: str
    verified: bool
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class DecisionRequired:
    decision: str
    why_required: str
    deadline: str | None
    risk_if_delayed: str
    recommended_option: str | None
    alternatives: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class PriorityNextWeek:
    title: str
    objective: str
    owner: str
    priority: str
    expected_impact: str
    deadline: str | None
    kpi_ids: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class WatchItem:
    title: str
    reason: str
    monitoring_metric: str
    trigger_condition: str
    owner: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedItem:
    title: str
    reason_unresolved: str
    owner: str
    next_action: str
    due_date: str | None
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class DataQuality:
    overall_status: str
    stale_sources: tuple[Mapping[str, Any], ...]
    missing_sources: tuple[str, ...]
    conflicting_sources: tuple[Mapping[str, Any], ...]
    unverified_metrics: tuple[str, ...]
    notes: str


def _tuplify(cls, raw: Mapping[str, Any]):
    vals = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        v = raw[f.name]
        vals[f.name] = tuple(v) if isinstance(v, list) else v
    return cls(**vals)


@dataclass(frozen=True)
class WeeklyReview:
    review_period_start: str
    review_period_end: str
    generated_at: str
    generated_by_agent: str
    organization_id: str
    authoritative: bool
    overall_health: Mapping[str, Any]
    sections: Mapping[str, DomainSection]
    top_wins: tuple[WinItem, ...]
    top_failures: tuple[FailureItem, ...]
    root_causes: tuple[RootCause, ...]
    decisions_required: tuple[DecisionRequired, ...]
    priorities_next_week: tuple[PriorityNextWeek, ...]
    recommendations: tuple[Mapping[str, Any], ...]   # existing recommendation contract (§7)
    watchlist: tuple[WatchItem, ...]
    unresolved_items: tuple[UnresolvedItem, ...]
    data_quality: DataQuality
    sources: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any], **validate_kw: Any) -> "WeeklyReview":
        result = validate_weekly_review(doc, **validate_kw)
        if not result.ok or "week_start" in doc:
            raise ValidationFailedError("invalid weekly review", result.errors or ["legacy v1 document"])
        return cls(
            review_period_start=doc["review_period_start"], review_period_end=doc["review_period_end"],
            generated_at=doc["generated_at"], generated_by_agent=doc["generated_by_agent"],
            organization_id=doc["organization_id"], authoritative=doc["authoritative"],
            overall_health=doc["overall_health"],
            sections={k: _tuplify(DomainSection, doc[k]) for k in WEEKLY_DOMAINS},
            top_wins=tuple(_tuplify(WinItem, x) for x in doc["top_wins"]),
            top_failures=tuple(_tuplify(FailureItem, x) for x in doc["top_failures"]),
            root_causes=tuple(_tuplify(RootCause, x) for x in doc["root_causes"]),
            decisions_required=tuple(_tuplify(DecisionRequired, x) for x in doc["decisions_required"]),
            priorities_next_week=tuple(_tuplify(PriorityNextWeek, x) for x in doc["priorities_next_week"]),
            recommendations=tuple(doc["recommendations"]),
            watchlist=tuple(_tuplify(WatchItem, x) for x in doc["watchlist"]),
            unresolved_items=tuple(_tuplify(UnresolvedItem, x) for x in doc["unresolved_items"]),
            data_quality=_tuplify(DataQuality, doc["data_quality"]),
            sources=tuple(doc["sources"]))

    @property
    def is_authoritative(self) -> bool:
        return self.authoritative and self.data_quality.overall_status == "GOOD" and not (
            self.data_quality.stale_sources or self.data_quality.missing_sources
            or self.data_quality.conflicting_sources) and all(
            s.status not in ("UNAVAILABLE", "PARTIAL") for s in self.sections.values())

    def to_dict(self) -> dict[str, Any]:
        def plain(v: Any) -> Any:
            if hasattr(v, "__dataclass_fields__"):
                return {k: plain(x) for k, x in asdict(v).items() if x is not None or k in ("deadline", "due_date")}
            if isinstance(v, tuple):
                return [plain(x) for x in v]
            return v
        out = {k: plain(getattr(self, k)) for k in ("review_period_start", "review_period_end", "generated_at",
                                                    "generated_by_agent", "organization_id", "authoritative",
                                                    "overall_health")}
        for k in WEEKLY_DOMAINS:
            sec = asdict(self.sections[k])
            out[k] = {kk: (list(vv) if isinstance(vv, tuple) else vv) for kk, vv in sec.items()
                      if vv is not None}
        for k in ("top_wins", "top_failures", "root_causes", "decisions_required", "priorities_next_week",
                  "watchlist", "unresolved_items"):
            out[k] = [{kk: (list(vv) if isinstance(vv, tuple) else vv) for kk, vv in asdict(x).items()}
                      for x in getattr(self, k)]
        out["recommendations"] = [dict(r) for r in self.recommendations]
        dq = asdict(self.data_quality)
        out["data_quality"] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in dq.items()}
        out["sources"] = [dict(s) for s in self.sources]
        return out
