"""Rate limiting (fixed windows) scoped by user, organization and endpoint category.

Limits come from organization policy `rate_limits` (conservative defaults otherwise). They sit
IN FRONT of the existing AI cost controls (budgets, per-run limits): command spam is refused
before a run is created, and budgets still apply to every run that is created.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Protocol

from .common import Clock, utc_now
from .errors import CommandCenterError


class RateLimitedError(CommandCenterError):
    def __init__(self, message: str, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Limit:
    name: str            # e.g. "commands:user"
    max_requests: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitPolicy:
    commands_per_user_per_minute: int = 6
    commands_per_org_per_hour: int = 200
    runs_per_org_per_hour: int = 300
    approvals_per_user_per_minute: int = 20
    api_requests_per_user_per_minute: int = 300

    @classmethod
    def from_mapping(cls, raw: Mapping[str, int] | None) -> "RateLimitPolicy":
        if not raw:
            return cls()
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"rate_limits: unknown keys {sorted(unknown)}")
        p = cls(**{k: int(v) for k, v in raw.items()})
        for k in cls.__dataclass_fields__:
            if getattr(p, k) <= 0:
                raise ValueError(f"rate_limits.{k} must be positive")
        return p

    def limits_for(self, category: str, *, user: str, org: str) -> list[tuple[str, Limit]]:
        base = [(f"api:user:{org}:{user}", Limit("api:user", self.api_requests_per_user_per_minute, 60))]
        if category == "command":
            base += [(f"cmd:user:{org}:{user}", Limit("commands:user", self.commands_per_user_per_minute, 60)),
                     (f"cmd:org:{org}", Limit("commands:org", self.commands_per_org_per_hour, 3600)),
                     (f"run:org:{org}", Limit("runs:org", self.runs_per_org_per_hour, 3600))]
        elif category == "approval":
            base.append((f"apr:user:{org}:{user}", Limit("approvals:user", self.approvals_per_user_per_minute, 60)))
        return base


class RateLimiter(Protocol):
    def hit(self, key: str, limit: Limit) -> None: ...


@dataclass
class InMemoryRateLimiter:
    clock: Clock = utc_now
    _counts: dict[tuple[str, int], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def hit(self, key: str, limit: Limit) -> None:
        now = self.clock()
        window = int(now.timestamp()) // limit.window_seconds
        with self._lock:
            n = self._counts.get((key, window), 0) + 1
            self._counts[(key, window)] = n
            if len(self._counts) > 100_000:      # drop old windows
                self._counts = {k: v for k, v in self._counts.items() if k[1] >= window - 1}
        if n > limit.max_requests:
            reset = datetime.fromtimestamp((window + 1) * limit.window_seconds, tz=now.tzinfo)
            raise RateLimitedError(f"rate limit {limit.name} exceeded ({limit.max_requests}/{limit.window_seconds}s)",
                                   max(1, int((reset - now) / timedelta(seconds=1))))


def check_limits(limiter: RateLimiter, policy: RateLimitPolicy, category: str, *, user: str, org: str) -> None:
    for key, limit in policy.limits_for(category, user=user, org=org):
        limiter.hit(key, limit)
