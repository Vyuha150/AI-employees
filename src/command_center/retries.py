"""Retry classes (Part 54).

MODEL_RETRY       model/provider calls. Retried per policy.retries.model.
READ_TOOL_RETRY   read tools are side-effect free, so they may be retried per policy.retries.read_tool.
WRITE_TOOL_RETRY  state-changing tools. Retried ONLY when the tool spec declares
                  `idempotent_with_execution_id` (the adapter de-duplicates on execution_id),
                  never after an EXECUTION_UNKNOWN on a RED action, and never beyond
                  policy.retries.write_tool.max_attempts (default 1 = no retry).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .common import RiskClass, StrEnum
from .policy import RetrySetting

T = TypeVar("T")


class RetryClass(StrEnum):
    MODEL_RETRY = "MODEL_RETRY"
    READ_TOOL_RETRY = "READ_TOOL_RETRY"
    WRITE_TOOL_RETRY = "WRITE_TOOL_RETRY"


@dataclass(frozen=True)
class RetryDecision:
    allowed: bool
    reason: str


def write_retry_allowed(*, idempotent: bool, risk: RiskClass, ambiguous: bool) -> RetryDecision:
    if ambiguous and risk is RiskClass.RED:
        return RetryDecision(False, "never retry a RED action after an ambiguous execution state")
    if ambiguous:
        return RetryDecision(False, "execution state is unknown; reconcile before any retry")
    if not idempotent:
        return RetryDecision(False, "tool does not guarantee idempotency")
    return RetryDecision(True, "idempotent tool with execution id")


def run_with_retries(
    fn: Callable[[int], T],
    setting: RetrySetting,
    should_retry: Callable[[T], bool],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[T, int]:
    """Call fn(attempt) up to setting.max_attempts while should_retry(result). Returns (result, attempts)."""
    attempt = 1
    result = fn(attempt)
    while should_retry(result) and attempt < setting.max_attempts:
        if setting.backoff_seconds:
            sleep(setting.backoff_seconds * (2 ** (attempt - 1)))
        attempt += 1
        result = fn(attempt)
    return result, attempt
