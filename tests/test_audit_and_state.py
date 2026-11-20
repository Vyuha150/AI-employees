import json
from dataclasses import FrozenInstanceError

import pytest

from command_center.audit import AuditEvent, AuditLogger, EventType, InMemoryAuditStore, JsonlAuditStore
from command_center.common import ActorType
from command_center.errors import AuditIntegrityError, InvalidTransitionError
from command_center.state_machines import (
    CHANGE_MACHINE,
    RECOMMENDATION_MACHINE,
    RUN_MACHINE,
    TASK_MACHINE,
    ChangeStatus,
    RecommendationStatus,
    RunStatus,
    TaskStatus,
)

from .conftest import ORG, FakeClock


def log(store, n=3):
    logger = AuditLogger(store, FakeClock())
    for i in range(n):
        logger.record(EventType.TASK_CREATED, organization_id=ORG, actor_type=ActorType.AGENT, actor_id="cfo",
                      object_type="task", object_id=f"t{i}", payload={"i": i, "nested": {"x": [1]}})
    return logger


def test_events_have_required_fields_and_chain():
    store = InMemoryAuditStore()
    log(store)
    events = store.events()
    assert [e.sequence for e in events] == [1, 2, 3]
    e = events[0]
    for f in ("event_id", "timestamp", "organization_id", "actor_type", "actor_id", "agent_id", "run_id",
              "event_type", "object_type", "object_id", "payload", "previous_state", "new_state"):
        assert hasattr(e, f)
    store.verify_chain()


def test_events_are_immutable_and_payload_is_a_copy():
    store = InMemoryAuditStore()
    log(store, 1)
    e = store.events()[0]
    with pytest.raises(FrozenInstanceError):
        e.payload_json = "{}"
    e.payload["nested"]["x"].append(99)
    assert store.events()[0].payload["nested"]["x"] == [1]


def test_store_has_no_update_or_delete():
    for cls in (InMemoryAuditStore, JsonlAuditStore):
        assert not any(hasattr(cls, m) for m in ("update", "delete", "remove", "clear"))


def test_tampering_is_detected():
    store = InMemoryAuditStore()
    log(store)
    original = store._events[1]
    store._events[1] = AuditEvent(**{**original.__dict__, "payload_json": json.dumps({"i": 999})})
    with pytest.raises(AuditIntegrityError, match="hash mismatch"):
        store.verify_chain()
    store._events[1] = original
    del store._events[1]
    with pytest.raises(AuditIntegrityError):
        store.verify_chain()


def test_presealed_events_rejected():
    store = InMemoryAuditStore()
    logger = log(store, 1)
    sealed = store.events()[0]
    with pytest.raises(AuditIntegrityError):
        logger.store.append(sealed)


def test_jsonl_store_persists_and_detects_tamper(tmp_path):
    path = tmp_path / "audit.jsonl"
    log(JsonlAuditStore(path))
    reopened = JsonlAuditStore(path)
    assert len(reopened.events()) == 3
    reopened.verify_chain()
    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["actor_id"] = "someone_else"
    lines[0] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(AuditIntegrityError):
        JsonlAuditStore(path)


def test_filters():
    store = InMemoryAuditStore()
    log(store)
    assert len(store.events(object_id="t1")) == 1
    assert len(store.events(object_id=["t0", "t2"])) == 2


# ---- state machines --------------------------------------------------------------------
@pytest.mark.parametrize("a, b", [
    (RunStatus.QUEUED, RunStatus.RUNNING), (RunStatus.RUNNING, RunStatus.TOOL_WAIT),
    (RunStatus.RUNNING, RunStatus.APPROVAL_REQUIRED), (RunStatus.RUNNING, RunStatus.SUCCEEDED),
    (RunStatus.RUNNING, RunStatus.FAILED), (RunStatus.RUNNING, RunStatus.CANCELLED),
    (RunStatus.APPROVAL_REQUIRED, RunStatus.RUNNING), (RunStatus.APPROVAL_REQUIRED, RunStatus.FAILED),
    (RunStatus.APPROVAL_REQUIRED, RunStatus.CANCELLED),
])
def test_run_allowed(a, b):
    RUN_MACHINE.check(a, b)


@pytest.mark.parametrize("a, b", [
    (RunStatus.SUCCEEDED, RunStatus.RUNNING), (RunStatus.FAILED, RunStatus.SUCCEEDED),
    (RunStatus.CANCELLED, RunStatus.RUNNING), (RunStatus.QUEUED, RunStatus.SUCCEEDED),
    (RunStatus.APPROVAL_REQUIRED, RunStatus.SUCCEEDED),
])
def test_run_forbidden(a, b):
    with pytest.raises(InvalidTransitionError):
        RUN_MACHINE.check(a, b)


def test_terminal_states_have_no_exits():
    for s in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
        assert RUN_MACHINE.is_terminal(s)
    for s in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        assert TASK_MACHINE.is_terminal(s)
    for s in (RecommendationStatus.REJECTED, RecommendationStatus.VERIFIED, RecommendationStatus.ROLLED_BACK):
        assert RECOMMENDATION_MACHINE.is_terminal(s)
    for s in (ChangeStatus.CLOSED, ChangeStatus.ROLLED_BACK):
        assert CHANGE_MACHINE.is_terminal(s)


def test_task_machine():
    TASK_MACHINE.check(TaskStatus.IN_PROGRESS, TaskStatus.WAITING_HUMAN)
    TASK_MACHINE.check(TaskStatus.BLOCKED, TaskStatus.IN_PROGRESS)
    with pytest.raises(InvalidTransitionError):
        TASK_MACHINE.check(TaskStatus.BACKLOG, TaskStatus.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        TASK_MACHINE.check(TaskStatus.COMPLETED, TaskStatus.IN_PROGRESS)


def test_recommendation_and_change_machines():
    with pytest.raises(InvalidTransitionError):
        RECOMMENDATION_MACHINE.check(RecommendationStatus.PROPOSED, RecommendationStatus.APPROVED)  # needs review
    with pytest.raises(InvalidTransitionError):
        CHANGE_MACHINE.check(ChangeStatus.DRAFT, ChangeStatus.IMPLEMENTATION)  # needs approval + planning
    CHANGE_MACHINE.check(ChangeStatus.TESTING, ChangeStatus.IMPLEMENTATION)
