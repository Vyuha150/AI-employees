from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from command_center.common import ActorType, Principal
from command_center.system import CommandCenter

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "src" / "command_center" / "resources" / "config" / "agents"
ORG = "org_example"


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def load_raw_profiles() -> dict[str, dict]:
    return {p.stem: yaml.safe_load(p.read_text()) for p in sorted(AGENT_DIR.glob("*.yaml"))}


@pytest.fixture
def raw_profiles():
    return copy.deepcopy(load_raw_profiles())


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cc(clock):
    return CommandCenter.build(clock=clock)


def human(pid: str, *, roles=(), permissions=(), org: str = ORG) -> Principal:
    return Principal(pid, org, ActorType.HUMAN, frozenset(roles), frozenset(permissions))


@pytest.fixture
def founder():
    agents = [p.stem for p in AGENT_DIR.glob("*.yaml")]
    return human("founder", roles={"FOUNDER", "MANAGER"},
                 permissions={f"run_agent:{a}" for a in agents} | {"auto_execute:GREEN"})


@pytest.fixture
def finance_approver():
    return human("fin_approver", roles={"FINANCE_APPROVER"})


@pytest.fixture
def manager():
    return human("ops_manager", roles={"MANAGER"})


def running_run(cc, agent_id, principal, objective="test objective"):
    run = cc.runs.create_run(agent_id, principal, objective)
    return cc.runs.start(run.run_id)


def action(tool, params, risk="GREEN", action_type=None, **extra):
    return {"action_id": extra.pop("action_id", "a1"), "action_type": action_type or tool, "tool": tool,
            "reason": extra.pop("reason", "test"), "parameters": params, "risk_class": risk,
            "financial_impact": extra.pop("financial_impact", None), "customer_impact": "none",
            "reversible": extra.pop("reversible", True),
            "approval_required": extra.pop("approval_required", risk != "GREEN"), **extra}
