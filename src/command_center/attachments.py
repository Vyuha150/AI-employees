"""Attachment contract (Part 46).

Attachments are never sent to a model directly. `check_model_access` must pass first:
same organization, the principal may read it, allowed MIME type, size limit, malware scan
CLEAN, and a classification the organization allows models to see. Text reaches the model
only through a DocumentExtractor, and every extracted text carries a source record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .common import Principal, StrEnum
from .policy import AttachmentPolicy
from .sources import AuthorityLevel, SourceRecord, SourceType


class ScanStatus(StrEnum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    FAILED = "FAILED"


class Classification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


@dataclass(frozen=True)
class Attachment:
    file_id: str
    organization_id: str
    name: str
    mime_type: str
    size: int
    storage_reference: str
    uploaded_by: str
    created_at: datetime
    classification: Classification
    scan_status: ScanStatus
    readable_by_roles: tuple[str, ...] = ("ALL",)


@dataclass(frozen=True)
class AccessCheck:
    allowed: bool
    reasons: tuple[str, ...]


def check_model_access(att: Attachment, principal: Principal, policy: AttachmentPolicy) -> AccessCheck:
    reasons = []
    if att.organization_id != principal.organization_id:
        reasons.append("attachment belongs to a different organization")
    if not ("ALL" in att.readable_by_roles or set(att.readable_by_roles) & set(principal.roles)):
        reasons.append("principal may not read this attachment")
    if att.mime_type not in policy.allowed_mime_types:
        reasons.append(f"MIME type {att.mime_type!r} is not allowed")
    if att.size <= 0 or att.size > policy.max_size_bytes:
        reasons.append(f"size {att.size} outside the allowed range (max {policy.max_size_bytes})")
    if att.scan_status is not ScanStatus.CLEAN:
        reasons.append(f"scan status is {att.scan_status.value}, not CLEAN")
    if att.classification.value not in policy.model_visible_classifications:
        reasons.append(f"classification {att.classification.value} may not be shown to models")
    return AccessCheck(not reasons, tuple(reasons))


class DocumentExtractor(Protocol):
    """Later: PDF/text/image extraction. Must return plain text and never execute content."""

    def extract(self, attachment: Attachment) -> str: ...


def attachment_source(att: Attachment, retrieved_at: datetime) -> SourceRecord:
    return SourceRecord(f"att:{att.file_id}", SourceType.DOCUMENT, att.name, f"attachment:{att.storage_reference}",
                        retrieved_at.isoformat(), AuthorityLevel.UNVERIFIED)
