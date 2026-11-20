"""Parts 29-33, 53: model providers, agent runtime, context assembly, provenance, fallback."""

import json
from types import SimpleNamespace

import pytest

from command_center.agents import RunContext
from command_center.audit import EventType
from command_center.common import ActorType
from command_center.errors import ConfigurationError
from command_center.model_providers import (
    ClaudeModelProvider,
    ModelProviderError,
    ModelSession,
    ToolCallRequest,
    ToolDefinition,
    mock_turn,
)
from command_center.state_machines import RunStatus

from .conftest import ORG
from .helpers import available_sources, contract_responder, mock_models, runtime_cc, structured


def ctx(principal, task="Review cash"):
    return RunContext("Example org", (), (), task, principal)


def start(cc, agent, principal, task="Review cash"):
    return cc.runs.create_run(agent, principal, task)


def test_runtime_happy_path_with_read_tool_through_gateway(clock, founder):
    cc, mock = runtime_cc(clock)
    cc.kpis.record({"kpi": "cash_runway", "value": "7", "unit": "months", "observed_at": "2026-11-19",
                    "source_reference": "erp:cash"}, organization_id=ORG, recorded_by="cfo")

    def final(session, last):
        res = json.loads(last)[0]
        assert res["status"] == "OK" and res["source_ids"]
        first = session.messages[0]["text"]
        srcs = [s for s in available_sources(first) if s["id"] == res["source_ids"][0]]
        return mock_turn(json.dumps(structured("cfo", srcs)))
    mock.script = [mock_turn("", [ToolCallRequest("c1", "read_kpis", {"kpis": ["cash_runway"]})]), final]
    run = start(cc, "cfo", founder)
    res = cc.runtime.execute(run.run_id, ctx(founder))
    assert res.status is RunStatus.SUCCEEDED and res.tool_calls == 1 and res.model_steps == 2, res.error
    assert cc.audit.store.events(event_type=EventType.TOOL_REQUESTED, run_id=run.run_id, object_id="read_kpis")
    assert cc.runs.get(run.run_id).model_name == "mock-model"
    usage = cc.usage.repo.list()
    assert len(usage) == 2 and all(u.estimated_cost is not None for u in usage)


def test_invented_source_is_rejected(clock, founder):
    fake = {"id": "s-invented", "type": "COMPANY_DATABASE", "reference": "crm:made-up"}
    cc, _ = runtime_cc(clock, script=[mock_turn(json.dumps(structured("cfo", [fake])))])
    run = start(cc, "cfo", founder)
    res = cc.runtime.execute(run.run_id, ctx(founder))
    assert res.status is RunStatus.FAILED and "provenance" in res.error
    out = cc.outputs.get(res.output_id)
    assert out.validity.value == "INVALID" and any("was not provided" in e for e in out.validation_errors)


def test_model_cannot_call_write_tools_directly(clock, founder):
    def final(session, last):
        res = json.loads(last)[0]
        assert res["status"] == "ERROR" and "not available as a direct tool" in res["error"]
        return mock_turn(json.dumps(structured("procurement", available_sources(session.messages[0]["text"]))))
    cc, mock = runtime_cc(clock, script=[mock_turn("", [ToolCallRequest("c1", "create_purchase_order",
                                                                        {"amount": 99, "currency": "INR"})]), final])
    run = start(cc, "procurement", founder)
    res = cc.runtime.execute(run.run_id, ctx(founder))
    assert res.status is RunStatus.SUCCEEDED
    assert cc.external_executor.calls == [] and cc.approvals.repo.list() == []


def test_proposed_actions_go_through_gateway_and_pause_for_approval(clock, founder):
    def respond(session, last):
        srcs = available_sources(session.messages[0]["text"])
        act = {"action_id": "a1", "action_type": "create_purchase_order", "tool": "create_purchase_order",
               "reason": "restock", "parameters": {"vendor_id": "v", "amount": 2_000_000, "currency": "INR"},
               "risk_class": "GREEN", "financial_impact": None, "customer_impact": "none", "reversible": False,
               "approval_required": False}
        return mock_turn(json.dumps(structured("procurement", srcs, proposed_actions=[act])))
    cc, _ = runtime_cc(clock, script=[respond])
    run = start(cc, "procurement", founder)
    res = cc.runtime.execute(run.run_id, ctx(founder))
    assert res.status is RunStatus.APPROVAL_REQUIRED
    (a,) = cc.approvals.repo.list()
    assert a.risk_class.value == "RED" and a.required_role == "FOUNDER"
    assert cc.audit.store.events(event_type=EventType.RISK_OVERRIDDEN_BY_POLICY, run_id=run.run_id)


def test_simulated_results_are_labelled_for_the_model(clock, founder):
    seen = {}

    def final(session, last):
        seen["results"] = json.loads(last)
        return mock_turn(json.dumps(structured("cfo", available_sources(session.messages[0]["text"]))))
    cc, _ = runtime_cc(clock, script=[mock_turn("", [ToolCallRequest("c1", "read_vendors", {})]), final],
                       read_connectors={"read_vendors": SimpleNamespace(fetch=lambda inv: __import__(
                           "command_center.executors", fromlist=["ToolResult"]).ToolResult(
                           True, "mock:1", {"mock": True}, mock=True, source_ids=("db:vendors:1",)))})
    run = start(cc, "cfo", founder)
    cc.runtime.execute(run.run_id, ctx(founder))
    assert seen["results"][0]["mock"] and "SIMULATED RESULT" in seen["results"][0]["notice"]


def test_refusal_and_invalid_json(clock, founder):
    cc, _ = runtime_cc(clock, script=[mock_turn("no", refused=True)])
    res = cc.runtime.execute(start(cc, "cfo", founder).run_id, ctx(founder))
    assert res.status is RunStatus.FAILED and "refused" in res.error
    cc, mock = runtime_cc(clock, script=[mock_turn("not json"), mock_turn("still not json")])
    res = cc.runtime.execute(start(cc, "cfo", founder).run_id, ctx(founder))
    assert res.status is RunStatus.FAILED and "not JSON" in res.error and len(mock.calls) == 2


def test_model_retry_then_no_silent_fallback(clock, founder):
    err = lambda: ModelProviderError("overloaded", retryable=True, provider="mock", model="mock-model")
    cc, mock = runtime_cc(clock, responder=contract_responder())
    mock.fail_with = [err()]
    res = cc.runtime.execute(start(cc, "cfo", founder).run_id, ctx(founder))
    assert res.status is RunStatus.SUCCEEDED and not res.fallback_used     # retried on the same model
    cc, mock = runtime_cc(clock, responder=contract_responder())
    mock.fail_with = [err(), err(), err()]
    res = cc.runtime.execute(start(cc, "cfo", founder).run_id, ctx(founder))
    assert res.status is RunStatus.FAILED and "model_failure" in res.error   # no fallback configured
    assert not cc.audit.store.events(event_type=EventType.MODEL_FALLBACK_USED)


def test_configured_fallback_is_audited_and_visible(clock, founder):
    cc, mock = runtime_cc(clock, responder=contract_responder(), models=mock_models(fallback=True))
    mock.fail_with = [ModelProviderError("down", retryable=False, provider="mock", model="mock-model")]
    run = start(cc, "cfo", founder)
    res = cc.runtime.execute(run.run_id, ctx(founder))
    assert res.status is RunStatus.SUCCEEDED and res.fallback_used and res.model == "backup-model"
    r = cc.runs.get(run.run_id)
    assert r.fallback_used and r.fallback_from == "mock/mock-model"
    assert cc.audit.store.events(event_type=EventType.MODEL_FALLBACK_USED, run_id=run.run_id)
    assert cc.usage.repo.list()[0].fallback_used                         # no pricing for backup -> cost NULL
    assert r.cost_unknown


def test_run_budget_exhaustion_mid_run_fails(clock, founder):
    big = mock_turn("", [ToolCallRequest("c1", "read_kpis", {})], input_tokens=150_000, output_tokens=0)
    cc, _ = runtime_cc(clock, script=[big])
    res = cc.runtime.execute(start(cc, "cfo", founder).run_id, ctx(founder))
    assert res.status is RunStatus.FAILED and "budget" in res.error


def test_disabled_or_foreign_context_rejected(clock, founder):
    cc, _ = runtime_cc(clock, responder=contract_responder())
    run = start(cc, "cfo", founder)
    other = founder.__class__("intruder", ORG, ActorType.HUMAN)
    with pytest.raises(Exception):
        cc.runtime.execute(run.run_id, ctx(other))


def test_context_is_scoped_and_sourced(clock, founder):
    from command_center.knowledge import KnowledgeInput
    cc, mock = runtime_cc(clock, responder=contract_responder())
    cc.knowledge.ingest(KnowledgeInput("Cash policy", "Minimum cash runway is six months.", "finance", "cfo", ("ALL",),
                                       "drive:cash-policy"), founder)
    cc.knowledge.ingest(KnowledgeInput("HR handbook", "cash bonus policy for staff", "people_policies", "hr",
                                       ("ALL",), "drive:hr"), founder)
    cc.knowledge.ingest(KnowledgeInput("Board pack", "Board cash discussion", "finance", "cfo", ("BOARD",),
                                       "drive:board"), founder)
    run = start(cc, "cfo", founder, "cash runway policy")
    cc.runtime.execute(run.run_id, ctx(founder, "cash runway policy"))
    sources = [s["reference"] for s in available_sources(mock.calls[0]["input"])]
    assert any("drive:cash-policy" in r for r in sources)
    assert not any("drive:hr" in r for r in sources)                # outside CFO knowledge scopes
    assert not any("drive:board" in r for r in sources)             # principal lacks BOARD role
    assert all(s["id"] and s["reference"] for s in available_sources(mock.calls[0]["input"]))


# ---- Claude provider (fake client; no network, no key) ---------------------------------
class FakeMessages:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def create(self, **kw):
        self.requests.append({**kw, "messages": list(kw.get("messages", []))})   # snapshot
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def response(content, stop="end_turn", model="claude-opus-5-5", **usage):
    u = SimpleNamespace(input_tokens=usage.get("i", 10), output_tokens=usage.get("o", 5),
                        cache_read_input_tokens=usage.get("cr", 3), cache_creation_input_tokens=usage.get("cw", 0))
    return SimpleNamespace(content=content, stop_reason=stop, usage=u, model=model)


def session(**kw):
    return ModelSession("anthropic", "claude-opus-5-5", 16000, "medium", "SYSTEM",
                        (ToolDefinition("read_kpis", "Read KPIs", {"type": "object", "properties": {}}),), **kw)


def test_claude_request_shape_tool_loop_and_usage():
    thinking = block("thinking", thinking="", signature="sig")
    fake = FakeMessages([response([thinking, block("tool_use", id="tu_1", name="read_kpis", input={"kpis": ["x"]})],
                                  "tool_use"),
                         response([block("text", text='{"ok": true}')], cr=7, cw=2)])
    p = ClaudeModelProvider(client=SimpleNamespace(messages=fake))
    s = session()
    t1 = p.generate_structured(s, "task", {"type": "object"})
    req = fake.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["output_config"] == {"effort": "medium"}
    assert req["tools"][0]["name"] == "read_kpis" and "format" not in req["output_config"]
    assert "thinking" not in req                          # adaptive thinking is the default
    assert t1.tool_calls[0].call_id == "tu_1" and t1.usage.cache_read_tokens == 3
    assert s.messages[1]["content"][0] is thinking          # full content (incl. thinking) kept
    t2 = p.continue_with_tool_result(s, t1, [{"tool_call_id": "tu_1", "status": "UNAVAILABLE"}])
    tr = fake.requests[1]["messages"][-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "tu_1" and tr["is_error"] is True
    assert t2.text == '{"ok": true}' and t2.usage.cache_write_tokens == 2 and not t2.fallback_used


def test_claude_refusal_and_error_mapping():
    import anthropic
    import httpx2
    fake = FakeMessages([response([], "refusal")])
    t = ClaudeModelProvider(client=SimpleNamespace(messages=fake)).generate(session(), "x")
    assert t.refused
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    rate = anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=req), body=None)
    bad = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=req), body=None)
    for exc, retryable in ((rate, True), (bad, False)):
        p = ClaudeModelProvider(client=SimpleNamespace(messages=FakeMessages([exc])))
        s = session()
        with pytest.raises(ModelProviderError) as info:
            p.generate(s, "x")
        assert info.value.retryable is retryable


def test_claude_key_from_environment_only():
    with pytest.raises(ConfigurationError, match="not set"):
        ClaudeModelProvider(env={})
    p = ClaudeModelProvider(env={"ANTHROPIC_API_KEY": "sk-ant-test-key-should-not-appear"})
    assert "sk-ant" not in repr(p) and "sk-ant" not in str(vars(p).get("_fallback"))


def test_claude_server_side_fallback_only_when_configured():
    beta = FakeMessages([response([block("text", text="{}")], model="claude-opus-4-8")])
    client = SimpleNamespace(messages=FakeMessages([]), beta=SimpleNamespace(messages=beta))
    t = ClaudeModelProvider(client=client, server_side_refusal_fallback=True).generate(session(), "x")
    assert beta.requests[0]["fallbacks"] == "default" and t.fallback_used and t.model == "claude-opus-4-8"
