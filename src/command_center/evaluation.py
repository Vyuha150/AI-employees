"""Human output evaluation (Part 14).

The composite score and the review decision are independent. A 5.0 composite does not
approve anything; only an explicit `decision` does, and the decision must be given.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from .common import StrEnum
from .errors import ValidationFailedError

WEIGHTS: dict[str, Decimal] = {
    "accuracy": Decimal("0.20"),
    "relevance": Decimal("0.15"),
    "completeness": Decimal("0.15"),
    "actionability": Decimal("0.20"),
    "business_value": Decimal("0.30"),
}
assert sum(WEIGHTS.values()) == Decimal("1.00")


class ReviewDecision(StrEnum):
    APPROVED = "APPROVED"
    NEEDS_REVISION = "NEEDS_REVISION"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class OutputEvaluation:
    output_id: str
    reviewer: str
    reviewed_at: datetime
    accuracy: int
    relevance: int
    completeness: int
    actionability: int
    business_value: int
    decision: ReviewDecision
    comment: str | None = None

    def __post_init__(self) -> None:
        errors = [f"{k} must be an integer from 1 to 5" for k in WEIGHTS
                  if not isinstance(getattr(self, k), int) or isinstance(getattr(self, k), bool)
                  or not 1 <= getattr(self, k) <= 5]
        if not isinstance(self.decision, ReviewDecision):
            errors.append("decision must be an explicit ReviewDecision")
        if self.decision in (ReviewDecision.REJECTED, ReviewDecision.NEEDS_REVISION) and not (self.comment or "").strip():
            errors.append("a rejection or revision request needs a comment")
        if errors:
            raise ValidationFailedError("invalid evaluation", errors)

    @property
    def composite_score(self) -> Decimal:
        total = sum(Decimal(getattr(self, k)) * w for k, w in WEIGHTS.items())
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def approved(self) -> bool:
        return self.decision is ReviewDecision.APPROVED

    @property
    def needs_revision(self) -> bool:
        return self.decision is ReviewDecision.NEEDS_REVISION

    @property
    def rejected(self) -> bool:
        return self.decision is ReviewDecision.REJECTED
