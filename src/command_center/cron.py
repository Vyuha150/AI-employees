"""Minimal, dependency-free 5-field cron (minute hour day-of-month month day-of-week).

Supports `*`, numbers, lists (`1,15`), ranges (`1-5`) and steps (`*/15`, `0-30/10`).
Day-of-week: 0-7, where 0 and 7 are Sunday. When both day-of-month and day-of-week are
restricted, a day matches if EITHER matches (standard cron semantics).

Times are evaluated in the job's IANA timezone:
- a local time that does not exist (DST spring-forward gap) is skipped for that day;
- an ambiguous local time (DST fall-back) fires once, at the first occurrence (fold=0).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


class CronError(ValueError):
    pass


_RANGES = {"minute": (0, 59), "hour": (0, 23), "dom": (1, 31), "month": (1, 12), "dow": (0, 7)}


def _field(text: str, name: str) -> frozenset[int]:
    lo, hi = _RANGES[name]
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"empty element in {name}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"invalid step in {name}")
            step = int(step_s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise CronError(f"invalid range in {name}")
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = b = int(part)
        else:
            raise CronError(f"invalid value {part!r} in {name}")
        if a < lo or b > hi or a > b:
            raise CronError(f"{name} value out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    if name == "dow" and 7 in out:
        out.discard(7)
        out.add(0)
    return frozenset(out)


@dataclass(frozen=True)
class CronExpression:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, text: str) -> "CronExpression":
        if not isinstance(text, str):
            raise CronError("cron must be a string")
        parts = text.split()
        if len(parts) != 5:
            raise CronError("cron must have 5 fields: minute hour day-of-month month day-of-week")
        m, h, dom, mon, dow = parts
        return cls(text, _field(m, "minute"), _field(h, "hour"), _field(dom, "dom"), _field(mon, "month"),
                   _field(dow, "dow"), dom != "*", dow != "*")

    def _day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.doms
        dow_ok = (d.isoweekday() % 7) in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, after: datetime, tz: str | ZoneInfo, *, max_days: int = 366 * 5) -> datetime:
        """First fire time strictly after `after` (aware), returned in UTC."""
        if after.tzinfo is None:
            raise CronError("`after` must be timezone-aware")
        zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        local_after = after.astimezone(zone)
        day = local_after.date()
        for _ in range(max_days):
            if self._day_matches(day):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        naive = datetime.combine(day, time(hour, minute))
                        candidate = naive.replace(tzinfo=zone, fold=0)
                        # skip non-existent local times (round trip through UTC changes the wall clock)
                        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
                            continue
                        if candidate > local_after:
                            return candidate.astimezone(timezone.utc)
            day += timedelta(days=1)
        raise CronError(f"no fire time for {self.text!r} within {max_days} days")

    def occurrences(self, start: datetime, end: datetime, tz: str | ZoneInfo, limit: int = 1000) -> list[datetime]:
        """Fire times in (start, end], at most `limit`."""
        out: list[datetime] = []
        cur = start
        while len(out) < limit:
            nxt = self.next_after(cur, tz)
            if nxt > end:
                break
            out.append(nxt)
            cur = nxt
        return out
