"""Agent output persistence contract.

Every structured response is stored, valid or not, so the audit trail shows what an agent
actually produced. Only VALID outputs may be ingested (turned into tasks, recommendations,
approvals or memory candidates). Content is stored as canonical JSON with its hash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from .agents import AgentRegistry
from .audit import AuditLogger, EventType
from .canonical import canonical_hash, canonical_json
from .common import ActorType, Clock, Principal, StrEnum, new_id, utc_now
from .errors import PermissionDeniedError, PolicyViolationError
from .evaluation import OutputEvaluation, ReviewDecision
from .runs import AgentRun
from .storage import InMemoryRepository, Repository
from .validate import validate_response


class OutputValidity(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass(frozen=True)
class AgentOutput:
    output_id: str
    organization_id: str
    run_id: str
    agent_id: str
    response_type: str | None
    content_json: str
    content_hash: str
    validity: OutputValidity
    validation_errors: tuple[str, ...]
    validation_warnings: tuple[str, ...]
    created_at: datetime
    ingested: bool = False
    evaluations: tuple[OutputEvaluation, ...] = ()
    review_decision: ReviewDecision | None = None
    version: int = 0

    @property
    def content(self) -> dict[str, Any]:
        return json.loads(self.content_json)


@dataclass
class OutputService:
    registry: AgentRegistry
    audit: AuditLogger
    repo: Repository[AgentOutput] = field(default_factory=lambda: InMemoryRepository("output_id"))
    clock: Clock = utc_now

    def get(self, output_id: str) -> AgentOutput:
        return self.repo.get(output_id)

    def record(self, run: AgentRun, response: Mapping[str, Any]) -> AgentOutput:
        agent = self.registry.get_agent(run.agent_id)
        result = validate_response(response, agent.allowed_tools, expected_agent_id=agent.agent_id)
        try:
            content_json = canonical_json(response)
        except ValueError as exc:
            content_json = json.dumps({"unserializable_response": str(exc)})
            result.errors.append(f"response is not canonical JSON: {exc}")
        out = AgentOutput(output_id=new_id("out"), organization_id=run.organization_id, run_id=run.run_id,
                          agent_id=run.agent_id,
                          response_type=response.get("response_type") if isinstance(response, Mapping) else None,
                          content_json=content_json, content_hash=canonical_hash(content_json),
                          validity=OutputValidity.VALID if result.ok else OutputValidity.INVALID,
                          validation_errors=tuple(result.errors), validation_warnings=tuple(result.warnings),
                          created_at=self.clock())
        self.repo.add(out)
        self.audit.record(EventType.OUTPUT_CREATED, organization_id=run.organization_id, actor_type=ActorType.AGENT,
                          actor_id=run.agent_id, agent_id=run.agent_id, run_id=run.run_id, object_type="output",
                          object_id=out.output_id,
                          payload={"validity": out.validity.value, "content_hash": out.content_hash,
                                   "errors": list(out.validation_errors), "warnings": list(out.validation_warnings)})
        return out

    def mark_ingested(self, output_id: str) -> AgentOutput:
        out = self.repo.get(output_id)
        if out.validity is not OutputValidity.VALID:
            raise PolicyViolationError("invalid outputs cannot be ingested")
        if out.ingested:
            raise PolicyViolationError("output already ingested")
        return self.repo.update(replace(out, ingested=True), expected_version=out.version)

    def review(self, evaluation: OutputEvaluation, reviewer: Principal) -> AgentOutput:
        if not reviewer.is_human or reviewer.principal_id != evaluation.reviewer:
            raise PermissionDeniedError("evaluations are recorded by the reviewing human")
        out = self.repo.get(evaluation.output_id)
        if reviewer.organization_id != out.organization_id:
            raise PermissionDeniedError("different organization")
        updated = self.repo.update(replace(out, evaluations=out.evaluations + (evaluation,),
                                           review_decision=evaluation.decision), expected_version=out.version)
        self.audit.record(EventType.OUTPUT_REVIEWED, organization_id=out.organization_id, actor_type=ActorType.HUMAN,
                          actor_id=reviewer.principal_id, agent_id=out.agent_id, run_id=out.run_id,
                          object_type="output", object_id=out.output_id,
                          payload={"composite_score": str(evaluation.composite_score),
                                   "decision": evaluation.decision.value,
                                   "scores": {k: getattr(evaluation, k) for k in
                                              ("accuracy", "relevance", "completeness", "actionability",
                                               "business_value")},
                                   "comment": evaluation.comment})
        return updated
