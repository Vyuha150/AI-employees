"""Parts 37-39: scheduler, enforcement policies, idempotent triggers, timezones."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from command_center.agents import AgentRegistry, ConcurrencyPolicy, MissedRunPolicy
from command_center.cron import CronError, CronExpression
from command_center.policy import OrganizationPolicy
from command_center.scheduler import ScheduledRunStatus
from command_center.system import CommandCenter
from command_center.workflow_catalog import KNOWN_WORKFLOWS

from .conftest import FakeClock, load_raw_profiles

UTC = timezone.utc


def make(clock, dispatched, *, policy=None, registry=None, active=lambda j: False):
    cc = CommandCenter.build(clock=clock, policy=policy, registry=registry)
    sched = cc.scheduler(lambda job, when: dispatched.append((job.job_id, when)) or f"ref-{job.job_id}",
                         is_active=active)
    sched.sync_jobs()
    return cc, sched


def test_all_required_workflows_are_scheduled(clock):
    _, s = make(clock, [])
    workflows = {j.workflow for j in s.jobs.list()}
    assert workflows == set(KNOWN_WORKFLOWS)
    j = s.jobs.get("daily_brief")
    assert j.timezone == "Asia/Kolkata" and j.missed_run_policy is MissedRunPolicy.SKIP
    assert j.concurrency_policy is ConcurrencyPolicy.FORBID
    # Fri 2026-11-20 14:30 IST -> next brief Mon 07:30 IST = 02:00 UTC
    assert j.next_run_at == datetime(2026, 11, 23, 2, 0, tzinfo=UTC)


def test_timezone_comes_from_organization_policy(clock):
    pol = OrganizationPolicy.load()
    other = replace(pol, timezone="America/New_York")
    _, s = make(clock, [], policy=other)
    j = s.jobs.get("daily_brief")
    assert j.timezone == "America/New_York"
    # clock is Fri 09:00 UTC = 04:00 in New York, so the next 07:30 EST brief is the same day
    assert j.next_run_at == datetime(2026, 11, 20, 12, 30, tzinfo=UTC)


def test_triggers_on_schedule_once_even_with_duplicate_ticks():
    clock = FakeClock(datetime(2026, 11, 23, 1, 0, tzinfo=UTC))      # Mon 06:30 IST
    seen = []
    _, s = make(clock, seen)
    clock.advance(hours=1, minutes=5)                                 # 07:35 IST
    first = s.tick()
    brief = [r for r in first if r.job_id == "daily_brief"]
    assert [r.status for r in brief] == [ScheduledRunStatus.TRIGGERED]
    assert ("daily_brief", datetime(2026, 11, 23, 2, 0, tzinfo=UTC)) in seen
    # a duplicate trigger (second scheduler instance with the same idempotency store)
    s.jobs.update(replace(s.jobs.get("daily_brief"), watermark=clock() - timedelta(hours=2)),
                  expected_version=s.jobs.get("daily_brief").version)
    again = [r for r in s.tick() if r.job_id == "daily_brief"]
    assert [r.status for r in again] == [ScheduledRunStatus.DUPLICATE]
    assert sum(1 for j, _ in seen if j == "daily_brief") == 1


def _job_policy(raws, agent, job_id, policy):
    for j in raws[agent]["schedule_policy"]["jobs"]:
        if j["job_id"] == job_id:
            j["missed_run_policy"] = policy
    return AgentRegistry.from_mappings(raws)


# 48h lookback: Mon-Wed occurrences are too old (skipped); Thu and Fri are eligible missed runs.
@pytest.mark.parametrize("policy, expected_runs", [("SKIP", 0), ("RUN_ONCE", 1), ("CATCH_UP", 2)])
def test_missed_run_policies(policy, expected_runs):
    clock = FakeClock(datetime(2026, 11, 23, 1, 0, tzinfo=UTC))        # Mon before the brief
    seen = []
    reg = _job_policy(load_raw_profiles(), "chief_of_staff", "daily_brief", policy)
    _, s = make(clock, seen, registry=reg)
    clock.advance(days=4, hours=6)                                       # Fri 12:30 IST: Mon-Fri briefs missed
    results = [r for r in s.tick() if r.job_id == "daily_brief"]
    triggered = [r for r in results if r.status is ScheduledRunStatus.TRIGGERED]
    assert len(triggered) == expected_runs
    assert len(results) == 5                                              # every occurrence accounted for
    if policy == "RUN_ONCE":
        assert triggered[0].scheduled_for == datetime(2026, 11, 27, 2, 0, tzinfo=UTC)
    assert len({r.scheduled_for for r in triggered}) == len(triggered)   # no duplicate catch-up runs


def test_catch_up_is_capped():
    clock = FakeClock(datetime(2026, 11, 23, 1, 0, tzinfo=UTC))
    reg = _job_policy(load_raw_profiles(), "chief_of_staff", "daily_brief", "CATCH_UP")
    pol = OrganizationPolicy.load()
    pol = replace(pol, scheduler=replace(pol.scheduler, lookback_hours=24 * 10))
    seen = []
    _, s = make(clock, seen, registry=reg, policy=pol)
    clock.advance(days=4, hours=6)
    results = [r for r in s.tick() if r.job_id == "daily_brief"]
    triggered = [r for r in results if r.status is ScheduledRunStatus.TRIGGERED]
    assert len(triggered) == pol.scheduler.max_catch_up_runs == 3
    assert [r.scheduled_for.day for r in triggered] == [23, 24, 25]      # oldest first
    assert sum(1 for r in results if r.status is ScheduledRunStatus.SKIPPED) == 2


def test_lookback_bounds_catch_up():
    clock = FakeClock(datetime(2026, 11, 23, 1, 0, tzinfo=UTC))
    reg = _job_policy(load_raw_profiles(), "chief_of_staff", "daily_brief", "CATCH_UP")
    seen = []
    _, s = make(clock, seen, registry=reg)
    clock.advance(days=14)
    triggered = [r for r in s.tick() if r.job_id == "daily_brief" and r.status is ScheduledRunStatus.TRIGGERED]
    assert len(triggered) <= 3
    assert all(clock() - r.scheduled_for <= timedelta(hours=48) for r in triggered)


def test_concurrency_forbid_skips_while_active():
    clock = FakeClock(datetime(2026, 11, 23, 1, 0, tzinfo=UTC))
    seen = []
    _, s = make(clock, seen, active=lambda job: job.job_id == "daily_brief")
    clock.advance(hours=1, minutes=5)
    brief = [r for r in s.tick() if r.job_id == "daily_brief"]
    assert brief[0].status is ScheduledRunStatus.SKIPPED and "FORBID" in brief[0].reason
    assert not any(j == "daily_brief" for j, _ in seen)


def test_disabled_agent_disables_its_jobs():
    clock = FakeClock(datetime(2026, 11, 23, 1, 0, tzinfo=UTC))
    raws = load_raw_profiles()
    raws["cfo"]["enabled"] = False
    seen = []
    _, s = make(clock, seen, registry=AgentRegistry.from_mappings(raws))
    clock.advance(hours=2)
    s.tick()
    assert not s.jobs.get("daily_finance_review").enabled
    assert not any(j == "daily_finance_review" for j, _ in seen)


def test_invalid_cron_rejected_by_profile_validation():
    raws = load_raw_profiles()
    raws["cfo"]["schedule_policy"]["jobs"][0]["cron"] = "61 * * * *"
    from command_center.errors import InvalidAgentProfileError
    with pytest.raises(InvalidAgentProfileError, match="cron"):
        AgentRegistry.from_mappings(raws).get_agent("cfo")
    with pytest.raises(CronError):
        CronExpression.parse("* * *")


def test_cron_dst_gap_and_dom_dow_semantics():
    c = CronExpression.parse("30 2 * * *")
    nxt = c.next_after(datetime(2026, 3, 7, 12, tzinfo=UTC), "America/New_York")
    assert nxt == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)                 # 2026-03-08 02:30 does not exist
    either = CronExpression.parse("0 9 1 * 1")                            # 1st of month OR Mondays
    assert either.next_after(datetime(2026, 11, 20, tzinfo=UTC), "UTC") == datetime(2026, 11, 23, 9, tzinfo=UTC)
