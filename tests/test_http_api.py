"""Phase 5: authenticated HTTP API, realtime, approvals, dashboards, errors, rate limits, health."""

import asyncio
import json
import uuid

import pytest

from command_center.http_api import build_routes
from command_center.openapi import openapi_document
from command_center.state_machines import ApprovalStatus, RunStatus

from .api_helpers import FULL, Stack, token
from .conftest import action, running_run

ORG = "org_a"


@pytest.fixture
def stack(clock):
    return Stack(clock)


def ok(resp, status=200):
    code, body, _ = resp
    assert code == status, body
    assert body["error"] is None and "request_id" in body["meta"]
    return body["data"]


def err(resp, status, code=None):
    s, body, headers = resp
    assert s == status, body
    assert body["data"] is None and body["error"]["code"] and body["error"]["message"]
    if code:
        assert body["error"]["code"] == code, body
    assert "Traceback" not in json.dumps(body)
    return body["error"]


# ---- authentication & identity ---------------------------------------------------------
def test_identity_comes_from_server_side_membership(stack):
    user = stack.member(ORG, roles=("MANAGER",), perms=("tasks:write",))
    forged = token(user, app_metadata={"roles": ["FOUNDER"], "organization_id": "org_b"},
                   user_metadata={"permissions": ["audit:read"]})
    me = ok(stack.call("GET", "/v1/me", tok=forged))
    assert me == {"user_id": user, "organization_id": ORG, "roles": ["MANAGER"], "permissions": ["tasks:write"]}
    err(stack.call("GET", "/v1/agents/cfo/performance", tok=forged), 403, "FORBIDDEN")   # claim ignored


@pytest.mark.parametrize("make, why", [
    (lambda u: None, "missing"),
    (lambda u: "not-a-jwt", "malformed"),
    (lambda u: token(u, exp_in=-3600), "expired"),
    (lambda u: token(u, secret="another-secret-that-is-long-enough-xyz"), "signature"),
    (lambda u: token(u, iss="https://evil.example/auth/v1"), "issuer"),
    (lambda u: token(u, aud="anon"), "audience"),
    (lambda u: token(u, role="anon"), "role"),
    (lambda u: token("not-a-uuid"), "subject"),
    (lambda u: __import__("jwt").encode({"sub": u, "exp": 9999999999, "iss": "x", "aud": "authenticated"}, None,
                                        algorithm="none"), "alg none"),
])
def test_invalid_tokens_are_401(stack, make, why):
    user = stack.member(ORG)
    tok = make(user)
    headers = {} if tok is None else {"authorization": f"Bearer {tok}"}
    err(stack.call("GET", "/v1/me", headers=headers), 401, "UNAUTHENTICATED")


def test_membership_and_organization_selection(stack):
    stranger = str(uuid.uuid4())
    err(stack.call("GET", "/v1/me", tok=token(stranger)), 403)
    multi = stack.member("org_a")
    stack.member("org_b", user=multi)
    err(stack.call("GET", "/v1/me", multi), 422, "ORGANIZATION_SELECTION_REQUIRED")
    assert ok(stack.call("GET", "/v1/me", multi, headers={"X-Organization": "org_b"}))["organization_id"] == "org_b"
    only_a = stack.member("org_a")
    err(stack.call("GET", "/v1/me", only_a, headers={"X-Organization": "org_b"}), 403)


def test_client_supplied_authority_is_rejected(stack):
    user = stack.member(ORG)
    for forged in ({"organization_id": "org_b"}, {"roles": ["FOUNDER"]}, {"permissions": ["audit:read"]}):
        e = err(stack.call("POST", "/v1/commands", user, {"requested_agent_id": "cfo", "message": "hi", **forged}),
                422, "CLIENT_SUPPLIED_AUTHORITY")
        assert "may not be supplied" in json.dumps(e["details"])
    err(stack.call("POST", "/v1/commands", user, {"requested_agent_id": "cfo", "message": "hi", "junk": 1}), 422)


# ---- commands & queued execution -------------------------------------------------------
def test_command_is_async_queued_and_idempotent(stack):
    user = stack.member(ORG)
    data = ok(stack.call("POST", "/v1/commands", user, {"requested_agent_id": "cfo", "message": "Cash position?",
                                                        "idempotency_key": "ui-1"}), 201)
    assert data["status"] == "QUEUED" and data["command_id"] != data["run_id"]
    cc = stack.ccs[ORG]
    assert cc.runs.get(data["run_id"]).status is RunStatus.QUEUED           # not executed inline
    dup = ok(stack.call("POST", "/v1/commands", user, {"requested_agent_id": "cfo", "message": "Cash position?",
                                                       "idempotency_key": "ui-1"}), 201)
    assert dup["status"] == "DUPLICATE" and dup["run_id"] == data["run_id"]
    status = ok(stack.call("GET", f"/v1/commands/{data['command_id']}", user))
    assert status["status"] == "QUEUED"
    stack.drain()
    status = ok(stack.call("GET", f"/v1/commands/{data['command_id']}", user))
    assert status["status"] == "SUCCEEDED" and status["run_status"] == "SUCCEEDED"
    msgs = ok(stack.call("GET", f"/v1/conversations/{data['conversation_id']}/messages", user))
    assert [m["sender_type"] for m in msgs["items"]] == ["HUMAN", "AGENT"]
    other = stack.member("org_b")
    err(stack.call("GET", f"/v1/commands/{data['command_id']}", other), 404)
    err(stack.call("GET", f"/v1/runs/{data['run_id']}", other), 404)
    err(stack.call("POST", "/v1/commands", user, {"requested_agent_id": "ghost", "message": "x"}), 404)


def test_command_rate_limit_precedes_cost_controls(stack):
    user = stack.member(ORG)
    limit = stack.ccs[ORG].policy.rate_limits.commands_per_user_per_minute
    for i in range(limit):
        ok(stack.call("POST", "/v1/commands", user, {"requested_agent_id": "cfo", "message": f"m{i}"}), 201)
    code, body, headers = stack.call("POST", "/v1/commands", user, {"requested_agent_id": "cfo", "message": "spam"})
    assert code == 429 and body["error"]["code"] == "RATE_LIMITED" and int(headers["retry-after"]) >= 1
    assert len(stack.ccs[ORG].runs.repo.list()) == limit                    # no run created for the refused call


# ---- realtime --------------------------------------------------------------------------
def test_realtime_events_cursor_and_isolation(stack):
    ua, ub = stack.member("org_a"), stack.member("org_b")
    ok(stack.call("POST", "/v1/commands", ua, {"requested_agent_id": "cfo", "message": "hi"}), 201)
    stack.drain()
    page = ok(stack.call("GET", "/v1/events", ua, query={"after": "0", "limit": "3"}))
    assert len(page["items"]) == 3 and page["next_cursor"] >= int(page["items"][-1]["id"])
    rest = ok(stack.call("GET", "/v1/events", ua, query={"after": str(page["next_cursor"])}))
    types = [e["event_type"] for e in page["items"] + rest["items"]]
    assert {"run.queued", "run.started", "output.created", "run.completed"} <= set(types)
    assert ok(stack.call("GET", "/v1/events", ub))["items"] == []          # other organization sees nothing
    blob = json.dumps(page) + json.dumps(rest)
    assert "Cash" not in blob and "parameters" not in blob


def asgi_call(app, path, headers=(), method="GET", body=b"", query=b""):
    out = {"status": None, "headers": {}, "chunks": []}

    async def run():
        sent = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if sent:
                return sent.pop(0)
            await asyncio.sleep(0.05)
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
            else:
                out["chunks"].append(msg.get("body", b""))
        scope = {"type": "http", "method": method, "path": path, "query_string": query,
                 "headers": [(k.encode(), v.encode()) for k, v in headers]}
        await app(scope, receive, send)
    asyncio.run(run())
    out["body"] = b"".join(out["chunks"])
    return out


def test_sse_stream_resume_cors_and_url_token_rejection(stack):
    from command_center.asgi import AsgiApp
    ua, ub = stack.member("org_a"), stack.member("org_b")
    ok(stack.call("POST", "/v1/commands", ua, {"requested_agent_id": "cfo", "message": "hi"}), 201)
    app = AsgiApp(stack.http, cors_origins=("https://app.lovable.example",), sse_poll_seconds=0.01,
                  sse_max_seconds=0.2)
    r = asgi_call(app, "/v1/events/stream", [("authorization", f"Bearer {token(ua)}"), ("last-event-id", "0")])
    assert r["status"] == 200 and r["headers"]["content-type"] == "text/event-stream"
    text = r["body"].decode()
    assert "event: run.queued" in text and "\nid: " in "\n" + text
    rb = asgi_call(app, "/v1/events/stream", [("authorization", f"Bearer {token(ub)}")])
    assert "event:" not in rb["body"].decode()
    bad = asgi_call(app, "/v1/events/stream", query=f"access_token={token(ua)}".encode())
    assert bad["status"] == 422
    pre = asgi_call(app, "/v1/me", [("origin", "https://app.lovable.example")], method="OPTIONS")
    assert pre["status"] == 204 and pre["headers"]["access-control-allow-origin"] == "https://app.lovable.example"
    evil = asgi_call(app, "/v1/me", [("origin", "https://evil.example")], method="OPTIONS")
    assert evil["status"] == 403
    me = asgi_call(app, "/v1/me", [("authorization", f"Bearer {token(ua)}")])
    assert me["status"] == 200 and me["headers"]["x-content-type-options"] == "nosniff"


# ---- approvals ---------------------------------------------------------------------------
def pending_po(stack, org=ORG, amount=5000):
    cc = stack.ccs[org]
    founder = next(iter(stack.directory.rows.values()))[0]
    from command_center.common import ActorType, Principal
    p = Principal(founder.user_id, org, ActorType.HUMAN, founder.roles, founder.permissions)
    run = running_run(cc, "procurement", p)
    o = cc.gateway.submit(run.run_id, action("create_purchase_order", {"vendor_id": "v", "amount": amount,
                                                                       "currency": "INR"}, risk="AMBER"))
    return o.approval_id


def test_approval_api_binds_payload_and_role(stack):
    stack.member(ORG)
    aid = pending_po(stack)
    approver = stack.member(ORG, roles=("FINANCE_APPROVER",), perms=())
    manager = stack.member(ORG, roles=("MANAGER",), perms=())
    listed = ok(stack.call("GET", "/v1/approvals", approver))
    assert [a["approval_id"] for a in listed] == [aid]
    detail = ok(stack.call("GET", f"/v1/approvals/{aid}", approver))
    assert detail["payload"]["amount"] == 5000 and len(detail["payload_hash"]) == 64
    e = err(stack.call("POST", f"/v1/approvals/{aid}/approve", approver, {"payload_hash": "0" * 64}), 409,
            "PAYLOAD_MISMATCH")
    assert e["details"]["expected_payload_hash"] == detail["payload_hash"]
    err(stack.call("POST", f"/v1/approvals/{aid}/approve", manager, {"payload_hash": detail["payload_hash"]}), 403)
    err(stack.call("POST", f"/v1/approvals/{aid}/approve", approver,
                   {"payload_hash": detail["payload_hash"], "required_role": "MANAGER"}), 422,
        "CLIENT_SUPPLIED_AUTHORITY")
    done = ok(stack.call("POST", f"/v1/approvals/{aid}/approve", approver, {"payload_hash": detail["payload_hash"]}))
    assert done["status"] == "APPROVED" and done["approved_by"] == approver
    err(stack.call("POST", f"/v1/approvals/{aid}/approve", approver, {"payload_hash": detail["payload_hash"]}), 409)
    other_org = stack.member("org_b", roles=("FINANCE_APPROVER",))
    err(stack.call("GET", f"/v1/approvals/{aid}", other_org), 404)
    assert stack.ccs[ORG].audit.store.events(object_type="approval", object_id=aid)


def test_reconciliation_api(clock):
    from command_center.executors import MockExternalExecutor
    stack = Stack(clock)
    stack.member(ORG)
    stack.ccs[ORG].gateway.executor.external = MockExternalExecutor(timeout_tools={"create_purchase_order"})
    aid = pending_po(stack)
    approver = stack.member(ORG, roles=("FINANCE_APPROVER",), perms=())
    h = ok(stack.call("GET", f"/v1/approvals/{aid}", approver))["payload_hash"]
    ok(stack.call("POST", f"/v1/approvals/{aid}/approve", approver, {"payload_hash": h}))
    a = stack.ccs[ORG].approvals.get(aid)
    stack.ccs[ORG].execute_approved(aid, tool_name=a.tool_name, action_type=a.action_type, payload=dict(a.payload))
    assert stack.ccs[ORG].approvals.get(aid).status is ApprovalStatus.OUTCOME_UNKNOWN
    events = [e["event_type"] for e in ok(stack.call("GET", "/v1/events", approver, query={"limit": "500"}))["items"]]
    assert "reconciliation.required" in events
    err(stack.call("POST", f"/v1/approvals/{aid}/reconcile", approver, {"outcome": "MAYBE", "evidence_ref": "x",
                                                                        "note": "y"}), 422)
    r = ok(stack.call("POST", f"/v1/approvals/{aid}/reconcile", approver,
                      {"outcome": "CONFIRMED_NOT_EXECUTED", "evidence_ref": "portal:none", "note": "no PO found"}))
    assert r["status"] == "FAILED" and r["reconciliation_outcome"] == "CONFIRMED_NOT_EXECUTED"
    new = ok(stack.call("POST", f"/v1/approvals/{aid}/reexecution", approver, {}))
    assert new["supersedes"] == aid and new["status"] == "PENDING"
    events = [e["event_type"] for e in ok(stack.call("GET", "/v1/events", approver, query={"limit": "500"}))["items"]]
    assert "reconciliation.completed" in events


# ---- conversations, tasks, outputs, recommendations ----------------------------------------
def test_conversation_and_task_endpoints(stack):
    user = stack.member(ORG)
    conv = ok(stack.call("POST", "/v1/conversations", user, {"title": "Pricing", "primary_agent_id": "sales_director"}),
              201)
    for i in range(4):
        ok(stack.call("POST", f"/v1/conversations/{conv['conversation_id']}/messages", user, {"content": f"m{i}"}))
    p1 = ok(stack.call("GET", f"/v1/conversations/{conv['conversation_id']}/messages", user, query={"limit": "3"}))
    p2 = ok(stack.call("GET", f"/v1/conversations/{conv['conversation_id']}/messages", user,
                       query={"after_seq": str(p1["next_cursor"])}))
    assert [m["content"] for m in p1["items"] + p2["items"]] == ["m0", "m1", "m2", "m3"]
    assert len(ok(stack.call("GET", "/v1/conversations", user))) == 1
    task = {"title": "Collect AR", "objective": "cash", "description": "call", "owner": "cfo", "priority": "P1",
            "expected_output": "dates", "completion_criteria": ["agreed"], "risk_level": "GREEN"}
    t = ok(stack.call("POST", "/v1/tasks", user, task), 201)
    err(stack.call("PATCH", f"/v1/tasks/{t['task_id']}", user, {"status": "COMPLETED"}), 409)   # state machine
    assert ok(stack.call("PATCH", f"/v1/tasks/{t['task_id']}", user, {"status": "IN_PROGRESS"}))["status"] == "IN_PROGRESS"
    viewer = stack.member(ORG, roles=(), perms=())
    err(stack.call("POST", "/v1/tasks", viewer, task), 403)
    page = stack.call("GET", "/v1/tasks", user, query={"limit": "1"})
    assert page[1]["meta"]["pagination"] == {"limit": 1, "offset": 0, "next_offset": 1}


def test_outputs_evaluations_and_recommendations(stack):
    from .test_end_to_end import response
    user = stack.member(ORG)
    cc = stack.ccs[ORG]
    from command_center.common import ActorType, Principal
    p = Principal(user, ORG, ActorType.HUMAN, frozenset({"FOUNDER"}), frozenset(FULL))
    run = running_run(cc, "procurement", p)
    rec = {"problem_or_opportunity": "Lead time rose.", "evidence": ["vendor history"], "evidence_source_ids": ["s1"],
           "recommendation": "Qualify a second vendor.", "why_now": "stock low", "expected_impact": "reliability",
           "impact_category": "OPERATIONAL_RELIABILITY", "estimated_value": None, "estimated_cost": None,
           "effort": "MEDIUM", "urgency": "HIGH", "dependencies": [], "risks": [], "reversible": True,
           "confidence": "MEDIUM", "suggested_owner": "procurement", "kpis_affected": ["supplier_reliability"]}
    out = cc.outputs.record(cc.runs.get(run.run_id), response("procurement", recs=[rec]))
    cc.ingest_output(out.output_id)
    assert ok(stack.call("GET", f"/v1/outputs/{out.output_id}/versions", user))[0]["version_no"] == 1
    ev = ok(stack.call("POST", f"/v1/outputs/{out.output_id}/evaluations", user,
                       {"accuracy": 5, "relevance": 5, "completeness": 5, "actionability": 5, "business_value": 5,
                        "decision": "NEEDS_REVISION", "comment": "wrong vendor list"}))
    assert ev["composite_score"] == "5.00" and ev["decision"] == "NEEDS_REVISION"     # score != approval
    err(stack.call("POST", f"/v1/outputs/{out.output_id}/evaluations", user,
                   {"accuracy": 9, "relevance": 5, "completeness": 5, "actionability": 5, "business_value": 5,
                    "decision": "APPROVED"}), 422)
    rev = ok(stack.call("POST", f"/v1/outputs/{out.output_id}/revision-requests", user, {"comment": "use v-22 too"}))
    assert cc.tasks.get(rev["task_id"]).owner == "procurement"
    (r,) = ok(stack.call("GET", "/v1/recommendations", user))
    rid = r["recommendation_id"]
    ok(stack.call("POST", f"/v1/recommendations/{rid}/reject", user, {"reason": "no budget"}))
    err(stack.call("POST", f"/v1/recommendations/{rid}/approve", user, {}), 409)        # rejected is terminal
    out2 = cc.outputs.record(cc.runs.get(run.run_id), response("procurement", recs=[rec]))
    report = cc.ingest_output(out2.output_id)
    assert any("no new evidence" in e for e in report.errors)                            # not silently recreated


def test_change_request_flow(stack):
    from .test_lifecycle import CHANGE, REC, SOURCES
    user = stack.member(ORG)
    cc = stack.ccs[ORG]
    rec = cc.recommendations.create(REC, organization_id=ORG, source_agent="lead_generation", run_id=None,
                                    sources=SOURCES)
    ok(stack.call("POST", f"/v1/recommendations/{rec.recommendation_id}/approve", user, {}))
    body = {k: v for k, v in CHANGE.items()}
    cr = ok(stack.call("POST", f"/v1/recommendations/{rec.recommendation_id}/change-requests", user, body))
    detail = ok(stack.call("GET", f"/v1/change-requests/{cr['change_id']}", user))
    assert detail["status"] == "DRAFT" and detail["lifecycle"][0]["to"] == "DRAFT"
    assert len(ok(stack.call("GET", "/v1/change-requests", user))) == 1


# ---- KPIs and dashboards -------------------------------------------------------------------
def test_kpis_and_dashboards(clock):
    from decimal import Decimal
    stack = Stack(clock, policy_overrides={"kpi_targets": {"cash_runway": {"target": Decimal("9"), "unit": "months"}}})
    user = stack.member(ORG)
    cc = stack.ccs[ORG]
    for v, d in (("6", "2026-10-31"), ("5", "2026-11-19")):
        cc.kpis.record({"kpi": "cash_runway", "value": v, "unit": "months", "observed_at": d,
                        "source_reference": "erp:cash"}, organization_id=ORG, recorded_by="cfo")
    kpis = {k["kpi"]: k for k in ok(stack.call("GET", "/v1/kpis", user))}
    cr = kpis["cash_runway"]
    assert cr["current_value"] == "5" and cr["target"] == "9" and cr["variance"] == "-4"
    assert cr["definition_source"] == "config/agents/cfo.yaml" and cr["freshness"]["stale"] is False
    assert kpis["revenue"]["current_value"] is None and kpis["revenue"]["target"] is None   # never invented
    hist = ok(stack.call("GET", "/v1/kpis/cash_runway/history", user))
    assert [h["value"] for h in hist] == ["5", "6"]
    err(stack.call("POST", "/v1/kpis", user, {"kpi": "x"}), 405)                           # no redefinition
    pending_po(stack)
    ex = ok(stack.call("GET", "/v1/dashboard/executive", user))
    assert ex["cash"]["kpis"][0]["value"] == "5" and ex["revenue"]["status"] == "UNAVAILABLE"
    assert ex["pending_approvals"]["count"] == 1 and ex["business_health"] is None
    assert {"risks", "priorities", "alerts", "data_freshness", "pipeline", "projects", "customers"} <= set(ex)
    ag = ok(stack.call("GET", "/v1/dashboard/agent/procurement", user))
    assert ag["status"] == "ACTIVE" and ag["active_run"] and len(ag["pending_approvals"]) == 1
    assert {"kpis", "recent_outputs", "performance_30d", "alerts", "usage_30d"} <= set(ag)
    err(stack.call("GET", "/v1/dashboard/agent/ghost", user), 404)


# ---- errors, health, openapi -------------------------------------------------------------
def test_error_envelopes(stack):
    user = stack.member(ORG)
    err(stack.call("GET", "/v1/nope", user), 404, "NOT_FOUND")
    err(stack.call("GET", "/v1/runs/00000000-0000-0000-0000-000000000000", user), 404)
    err(stack.call("POST", "/v1/conversations", user, b"{not json"), 422)
    err(stack.call("GET", "/v1/runs", user, query={"limit": "abc"}), 422)
    from command_center.http_api import Route

    def boom(ctx):
        raise RuntimeError("secret internal detail /etc/passwd")
    stack.http.routes.append(Route("GET", "/v1/boom", boom, "test", "test"))
    e = err(stack.call("GET", "/v1/boom", user), 500, "INTERNAL_ERROR")
    assert "secret internal detail" not in json.dumps(e)


def test_health_endpoints(stack):
    assert ok(stack.call("GET", "/health/live")) == {"status": "live"}
    assert ok(stack.call("GET", "/health/ready"))["status"] == "ready"
    stack.http.health = lambda: (False, {"database": "unreachable"})
    code, body, _ = stack.call("GET", "/health/ready")
    assert code == 503 and body["data"]["status"] == "not_ready"


def test_operations_endpoints_need_permission(stack):
    viewer = stack.member(ORG, roles=(), perms=())
    err(stack.call("GET", "/v1/connectors/status", viewer), 403)
    ops = stack.member(ORG, perms=("operations:read",))
    rows = ok(stack.call("GET", "/v1/connectors/status", ops))
    assert len(rows) == 6 and all(r["state"] == "UNAVAILABLE" for r in rows)


def test_openapi_and_frontend_docs_are_current():
    from pathlib import Path
    from command_center.openapi import frontend_markdown
    root = Path(__file__).resolve().parents[1]
    doc = openapi_document(build_routes())
    assert json.loads((root / "docs" / "openapi.json").read_text()) == json.loads(json.dumps(doc))
    assert (root / "docs" / "frontend_api.md").read_text() == frontend_markdown(build_routes())
    for r in build_routes():
        assert r.path in doc["paths"] and r.method.lower() in doc["paths"][r.path]
