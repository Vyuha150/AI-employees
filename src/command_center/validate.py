"""Application-side governance checks for STRUCTURED MODE agent responses.

The schema enforces shape (§25). These checks enforce the rules a schema cannot express:
tool allow-listing (§5), approval coverage for AMBER/RED actions (§6, §26),
and source traceability for facts and memory candidates (§3, §19).

Passing validation means a response is *admissible*. It is never an approval and
never an execution (§30).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "structured_response.schema.json"
_VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_response(
    response: Mapping[str, Any],
    allowed_tools: Iterable[str],
    expected_agent_id: str | None = None,
) -> ValidationResult:
    result = ValidationResult()

    for err in sorted(_VALIDATOR.iter_errors(response), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        result.errors.append(f"schema: {loc}: {err.message}")
    if result.errors:
        return result  # semantic checks assume a well-formed response

    if expected_agent_id is not None and response["agent_id"] != expected_agent_id:
        result.errors.append(f"agent_id {response['agent_id']!r} does not match {expected_agent_id!r}")

    allowed = set(allowed_tools)
    actions = response["proposed_actions"]
    action_ids = [a["action_id"] for a in actions]
    if len(action_ids) != len(set(action_ids)):
        result.errors.append("proposed_actions contain duplicate action_id values")

    approvals = {r["action_id"]: r for r in response["approval_requests"]}
    for action in actions:
        aid = action["action_id"]
        if action["tool"] not in allowed:
            result.errors.append(f"action {aid}: tool {action['tool']!r} is not in ALLOWED_TOOLS")
        if action["approval_required"] and aid not in approvals:
            result.errors.append(f"action {aid}: approval_required but no approval_request references it")
        if aid in approvals and approvals[aid]["risk_class"] != action["risk_class"]:
            result.errors.append(f"action {aid}: approval_request risk_class differs from the action's")
        if action["risk_class"] == "RED" and action["reversible"] is False:
            result.warnings.append(f"action {aid}: irreversible RED action — requires explicit authorized approval")

    for aid in approvals.keys() - set(action_ids):
        result.errors.append(f"approval_request references unknown action_id {aid!r}")

    source_ids = {s["id"] for s in response["sources"]}

    def check_refs(label: str, refs: Iterable[str]) -> None:
        for ref in refs:
            if ref not in source_ids:
                result.errors.append(f"{label}: source_id {ref!r} is not listed in sources")

    for i, fact in enumerate(response["facts"]):
        refs = fact.get("source_ids", [])
        if not refs:
            result.errors.append(f"facts/{i}: a FACT must cite at least one source (§3)")
        check_refs(f"facts/{i}", refs)
    for i, finding in enumerate(response["findings"]):
        refs = finding.get("source_ids", [])
        if finding["kind"] == "FACT" and not refs:
            result.errors.append(f"findings/{i}: a FACT finding must cite at least one source (§3)")
        check_refs(f"findings/{i}", refs)
    for i, mem in enumerate(response["memory_candidates"]):
        check_refs(f"memory_candidates/{i}", mem["source_ids"])
        if mem.get("scope") in ("CUSTOMER", "PROJECT") and not mem.get("scope_ref"):
            result.errors.append(f"memory_candidates/{i}: {mem['scope']} scope requires scope_ref (§17)")
    for i, rec in enumerate(response["recommendations"]):
        refs = rec.get("evidence_source_ids", [])
        check_refs(f"recommendations/{i}", refs)
        if not refs:
            result.warnings.append(f"recommendations/{i}: evidence cites no source ids (§16)")

    if response["response_type"] == "DAILY_BRIEF" and len(response["tasks"]) > 7:
        result.warnings.append("daily brief lists more than 7 priorities (§17)")
    if len(response["recommendations"]) > 5:
        result.warnings.append("more than 5 recommendations — prefer fewer, stronger ones (§28)")

    return result
