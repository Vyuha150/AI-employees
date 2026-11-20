"""Source and evidence model (Part 16).

The structured-response schema uses `id` / `type` on the wire (unchanged, so existing
outputs stay valid). This module normalizes them to `source_id` / `source_type`.
Legacy wire types map onto the canonical ones:
  DATABASE -> COMPANY_DATABASE, USER -> USER_MESSAGE, WEB -> WEB_SOURCE,
  KNOWLEDGE -> DOCUMENT, MEMORY -> ACCEPTED_MEMORY, AGENT -> AGENT_OUTPUT.
ACCEPTED_MEMORY and AGENT_OUTPUT are additions to the seven requested types: an accepted
memory or a delegated specialist's output is citeable, but neither is primary evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .common import StrEnum
from .errors import ValidationFailedError


class SourceType(StrEnum):
    COMPANY_DATABASE = "COMPANY_DATABASE"
    DOCUMENT = "DOCUMENT"
    USER_MESSAGE = "USER_MESSAGE"
    TOOL_RESULT = "TOOL_RESULT"
    WEB_SOURCE = "WEB_SOURCE"
    SYSTEM_CONFIG = "SYSTEM_CONFIG"
    HUMAN_DECISION = "HUMAN_DECISION"
    ACCEPTED_MEMORY = "ACCEPTED_MEMORY"
    AGENT_OUTPUT = "AGENT_OUTPUT"


class AuthorityLevel(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"   # system of record or explicit human decision
    SECONDARY = "SECONDARY"           # derived or summarized
    UNVERIFIED = "UNVERIFIED"         # external or unconfirmed


LEGACY_SOURCE_TYPES: Mapping[str, SourceType] = {
    "DATABASE": SourceType.COMPANY_DATABASE,
    "USER": SourceType.USER_MESSAGE,
    "WEB": SourceType.WEB_SOURCE,
    "KNOWLEDGE": SourceType.DOCUMENT,
    "MEMORY": SourceType.ACCEPTED_MEMORY,
    "AGENT": SourceType.AGENT_OUTPUT,
}

PRIMARY_EVIDENCE = frozenset(SourceType) - {SourceType.ACCEPTED_MEMORY, SourceType.AGENT_OUTPUT}


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    source_type: SourceType
    title: str | None
    reference: str
    retrieved_at: str | None = None
    authority_level: AuthorityLevel | None = None

    @property
    def is_primary_evidence(self) -> bool:
        return self.source_type in PRIMARY_EVIDENCE

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "SourceRecord":
        t = raw["type"]
        st = LEGACY_SOURCE_TYPES.get(t) or SourceType(t)
        al = raw.get("authority_level")
        return cls(raw["id"], st, raw.get("title"), raw["reference"], raw.get("retrieved_at"),
                   AuthorityLevel(al) if al else None)


class SourceCatalog:
    """The set of sources a single output (or run) may cite."""

    def __init__(self, sources: Iterable[SourceRecord]):
        self._by_id: dict[str, SourceRecord] = {}
        for s in sources:
            if s.source_id in self._by_id:
                raise ValidationFailedError(f"duplicate source_id {s.source_id!r}")
            self._by_id[s.source_id] = s

    @classmethod
    def from_response(cls, response: Mapping[str, Any]) -> "SourceCatalog":
        return cls(SourceRecord.from_wire(s) for s in response.get("sources", []))

    def get(self, source_id: str) -> SourceRecord:
        return self._by_id[source_id]

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._by_id

    def missing(self, refs: Iterable[str]) -> list[str]:
        return [r for r in refs if r not in self._by_id]

    def require(self, refs: Iterable[str], label: str, *, min_refs: int = 1) -> None:
        refs = list(refs)
        if len(refs) < min_refs:
            raise ValidationFailedError(f"{label}: at least {min_refs} source reference(s) required")
        missing = self.missing(refs)
        if missing:
            raise ValidationFailedError(f"{label}: unknown source ids", missing)
