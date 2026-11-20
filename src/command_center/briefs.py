"""Daily brief (Part 19, §17) and weekly review (Part 20) contracts.

Schema validation plus rules a schema cannot express:
- at most 7 priorities (schema), a warning above the preferred 5;
- every cited source_id must resolve to a listed source;
- a priority's source_agent must be a registered agent;
- a verified root cause must cite evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

from .validate import ValidationResult

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
_DAILY = Draft202012Validator(json.loads((_SCHEMAS / "daily_brief.schema.json").read_text(encoding="utf-8")))
_WEEKLY = Draft202012Validator(json.loads((_SCHEMAS / "weekly_review.schema.json").read_text(encoding="utf-8")))

MAX_PRIORITIES = 7
PREFERRED_PRIORITIES = 5


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


def validate_daily_brief(doc: Mapping[str, Any], known_agents: Iterable[str] | None = None) -> ValidationResult:
    result = ValidationResult(errors=_schema_errors(_DAILY, doc))
    if result.errors:
        return result
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


def validate_weekly_review(doc: Mapping[str, Any]) -> ValidationResult:
    result = ValidationResult(errors=_schema_errors(_WEEKLY, doc))
    if result.errors:
        return result
    _check_refs(doc, result)
    if doc["week_end"] < doc["week_start"]:
        result.errors.append("week_end is before week_start")
    for i, rc in enumerate(doc["root_causes"]):
        if rc["status"] == "VERIFIED" and not rc.get("evidence_source_ids"):
            result.errors.append(f"root_causes/{i}: a VERIFIED root cause must cite evidence")
    return result
