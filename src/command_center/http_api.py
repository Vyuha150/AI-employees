"""Framework-neutral HTTP API core for the frontend (Lovable).

    HttpApi.handle(HttpRequest) -> HttpResponse

Request pipeline: route match -> authenticate (Supabase JWT) -> resolve principal from server-side
memberships -> rate limits -> validate request (strict schemas; client-supplied organization_id /
roles / permissions are rejected) -> call ApiService (existing domain permission checks) ->
envelope. Every response is {"data", "meta", "error"}; failures never include stack traces.

Adapters: `asgi.py` (uvicorn) wraps this core; tests call `handle()` directly.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Mapping

from .api import ApiService, to_json
from .auth import (
    AuthenticationError,
    MembershipDirectory,
    MembershipError,
    OrganizationSelectionRequired,
    TokenVerifier,
    resolve_principal,
)
from .common import Principal, new_id
from .errors import (
    AgentDisabledError,
    ApprovalError,
    ApprovalNotExecutableError,
    BudgetExceededError,
    ConcurrencyError,
    InvalidAgentProfileError,
    InvalidTransitionError,
    NotFoundError,
    PayloadMismatchError,
    PermissionDeniedError,
    PolicyViolationError,
    UnknownAgentError,
    ValidationFailedError,
)
from .ratelimit import RateLimitedError, RateLimiter, check_limits

log = logging.getLogger("command_center.api")

FORBIDDEN_CLIENT_FIELDS = frozenset({"organization_id", "org_id", "roles", "role", "permissions", "user_id",
                                     "principal", "approver_role", "required_role", "approved_by"})


# ---------------------------------------------------------------------------------------
# request / response / errors
# ---------------------------------------------------------------------------------------
@dataclass
class HttpRequest:
    method: str
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)     # lower-case names
    body: bytes = b""
    client_ip: str | None = None


@dataclass
class HttpResponse:
    status: int
    body: Mapping[str, Any] | None
    headers: dict[str, str] = field(default_factory=dict)
    stream: Any = None            # async iterator of bytes (SSE), handled by the ASGI adapter


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details, self.headers = status, code, message, details, headers or {}


ERROR_CODES = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 422: "VALIDATION_FAILED",
               429: "RATE_LIMITED", 500: "INTERNAL_ERROR"}


def map_exception(exc: Exception) -> ApiError:
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, AuthenticationError):
        return ApiError(401, "UNAUTHENTICATED", str(exc))
    if isinstance(exc, OrganizationSelectionRequired):
        return ApiError(422, "ORGANIZATION_SELECTION_REQUIRED", str(exc))
    if isinstance(exc, (MembershipError, PermissionDeniedError, AgentDisabledError)):
        return ApiError(403, "FORBIDDEN", str(exc))
    if isinstance(exc, (NotFoundError, UnknownAgentError)):
        return ApiError(404, "NOT_FOUND", str(exc))
    if isinstance(exc, PayloadMismatchError):
        return ApiError(409, "PAYLOAD_MISMATCH", str(exc))
    if isinstance(exc, BudgetExceededError):
        return ApiError(429, "BUDGET_EXCEEDED", str(exc))
    if isinstance(exc, RateLimitedError):
        return ApiError(429, "RATE_LIMITED", str(exc), headers={"retry-after": str(exc.retry_after_seconds)})
    if isinstance(exc, (ConcurrencyError, InvalidTransitionError, ApprovalNotExecutableError, ApprovalError,
                        PolicyViolationError)):
        return ApiError(409, "CONFLICT", str(exc))
    if isinstance(exc, ValidationFailedError):
        return ApiError(422, "VALIDATION_FAILED", str(exc), {"errors": exc.errors})
    if isinstance(exc, (ValueError, KeyError, InvalidAgentProfileError)):
        return ApiError(422, "VALIDATION_FAILED", str(exc) if not isinstance(exc, KeyError) else f"missing {exc}")
    return ApiError(500, "INTERNAL_ERROR", "internal error")


def envelope(data: Any, meta: Mapping[str, Any] | None = None, error: ApiError | None = None) -> dict:
    if error is not None:
        return {"data": None, "meta": dict(meta or {}),
                "error": {"code": error.code, "message": error.message, "details": error.details}}
    return {"data": data, "meta": dict(meta or {}), "error": None}


# ---------------------------------------------------------------------------------------
# schemas (validation + OpenAPI)
# ---------------------------------------------------------------------------------------
def obj(required: Mapping[str, Any] | None = None, optional: Mapping[str, Any] | None = None) -> dict:
    props = {**(required or {}), **(optional or {})}
    return {"type": "object", "additionalProperties": False, "required": sorted(required or {}), "properties": props}


STR = {"type": "string", "minLength": 1}
OPT_STR = {"type": ["string", "null"]}
INT = {"type": "integer"}
STR_LIST = {"type": "array", "items": {"type": "string"}}
ANY_OBJ = {"type": "object"}
SCORE = {"type": "integer", "minimum": 1, "maximum": 5}

_PY_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict, "null": type(None),
             "number": (int, float)}


def validate_body(schema: Mapping[str, Any], body: Any) -> list[str]:
    if not isinstance(body, dict):
        return ["request body must be a JSON object"]
    errors = [f"{k}: may not be supplied by the client (derived from the authenticated session)"
              for k in body if k in FORBIDDEN_CLIENT_FIELDS]
    props = schema.get("properties", {})
    for k in schema.get("required", []):
        if k not in body:
            errors.append(f"{k}: required")
    for k, v in body.items():
        if k in FORBIDDEN_CLIENT_FIELDS:
            continue
        if k not in props:
            errors.append(f"{k}: unknown field")
            continue
        types = props[k].get("type")
        if types is None:
            continue
        types = types if isinstance(types, list) else [types]
        if not any(isinstance(v, _PY_TYPES[t]) and not (t == "integer" and isinstance(v, bool)) for t in types):
            errors.append(f"{k}: expected {'/'.join(types)}")
        elif isinstance(v, str) and props[k].get("minLength") and len(v) < props[k]["minLength"]:
            errors.append(f"{k}: must not be empty")
        elif isinstance(v, int) and not isinstance(v, bool):
            if "minimum" in props[k] and v < props[k]["minimum"] or "maximum" in props[k] and v > props[k]["maximum"]:
                errors.append(f"{k}: out of range")
    return errors


@dataclass
class Ctx:
    principal: Principal
    api: ApiService
    params: Mapping[str, str]
    query: Mapping[str, str]
    body: dict
    request_id: str

    def q_int(self, name: str, default: int, maximum: int = 500) -> int:
        try:
            v = int(self.query.get(name, default))
        except ValueError:
            raise ApiError(422, "VALIDATION_FAILED", f"{name} must be an integer") from None
        if v < 0:
            raise ApiError(422, "VALIDATION_FAILED", f"{name} must be >= 0")
        return min(v, maximum)


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: Callable[[Ctx], Any]
    summary: str
    tag: str
    auth: bool = True
    body: Mapping[str, Any] | None = None
    query: tuple[str, ...] = ()
    paginated: bool = False
    rate: str = "api"                 # api | command | approval | none
    realtime: tuple[str, ...] = ()
    response: str = "object"          # object | list | page

    @property
    def pattern(self) -> re.Pattern:
        return re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.path) + "$")


def _page(ctx: Ctx, rows: list, limit: int, offset: int) -> tuple[list, dict]:
    return rows, {"pagination": {"limit": limit, "offset": offset,
                                 "next_offset": offset + limit if len(rows) == limit else None}}


def _paged(fn: Callable[..., list]) -> Callable[[Ctx], Any]:
    def run(ctx: Ctx) -> Any:
        limit, offset = ctx.q_int("limit", 50, 200), ctx.q_int("offset", 0, 10**9)
        if limit == 0:
            raise ApiError(422, "VALIDATION_FAILED", "limit must be >= 1")
        return _page(ctx, fn(ctx, limit, offset), limit, offset)
    return run


def _date(value: str | None, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ApiError(422, "VALIDATION_FAILED", f"{name} must be an ISO date (YYYY-MM-DD)") from None


# ---------------------------------------------------------------------------------------
# route table
# ---------------------------------------------------------------------------------------
def build_routes() -> list[Route]:
    R = Route
    a = lambda c: c.api
    p = lambda c: c.principal
    return [
        # identity
        R("GET", "/v1/me", lambda c: {"user_id": c.principal.principal_id, "organization_id": c.principal.organization_id,
                                       "roles": sorted(c.principal.roles), "permissions": sorted(c.principal.permissions)},
          "Current user, organization, roles and permissions (server-resolved)", "identity"),
        # agents
        R("GET", "/v1/agents", lambda c: a(c).list_agents(p(c)), "List agents", "agents", response="list"),
        R("GET", "/v1/agents/{agent_id}", lambda c: a(c).get_agent(p(c), c.params["agent_id"]), "Get agent", "agents"),
        R("GET", "/v1/agents/{agent_id}/performance",
          lambda c: a(c).get_agent_performance(p(c), c.params["agent_id"], c.query.get("period", "30d")),
          "Agent performance (7d/30d/90d); needs performance:read", "agents", query=("period",)),
        # runs
        R("GET", "/v1/runs", _paged(lambda c, lim, off: a(c).list_runs(p(c), agent_id=c.query.get("agent_id"),
                                                                        status=c.query.get("status"), limit=lim,
                                                                        offset=off)),
          "List runs", "runs", query=("agent_id", "status", "limit", "offset"), paginated=True, response="list"),
        R("GET", "/v1/runs/{run_id}", lambda c: a(c).get_run(p(c), c.params["run_id"]), "Get run", "runs"),
        R("POST", "/v1/runs/{run_id}/cancel", lambda c: a(c).cancel_run(p(c), c.params["run_id"], c.body["reason"]),
          "Cancel a run (initiator or runs:cancel)", "runs", body=obj({"reason": STR}), realtime=("run.cancelled",)),
        # commands
        R("POST", "/v1/commands", lambda c: _command(c), "Submit a command (async; returns command/run ids)", "commands",
          body=obj({"requested_agent_id": STR, "message": STR},
                   {"conversation_id": OPT_STR, "attachment_ids": STR_LIST, "context_selection": {"type": "array"},
                    "idempotency_key": OPT_STR}),
          rate="command", realtime=("run.queued", "run.started", "run.completed", "run.failed")),
        R("GET", "/v1/commands/{command_id}", lambda c: a(c).get_command(p(c), c.params["command_id"]),
          "Command status", "commands"),
        # conversations
        R("GET", "/v1/conversations", lambda c: a(c).list_conversations(p(c), limit=c.q_int("limit", 50, 200)),
          "List conversations (most recent first)", "conversations", query=("limit",), response="list"),
        R("POST", "/v1/conversations", lambda c: a(c).create_conversation(
            p(c), title=c.body["title"], primary_agent_id=c.body.get("primary_agent_id")),
          "Create a conversation", "conversations", body=obj({"title": STR}, {"primary_agent_id": OPT_STR})),
        R("GET", "/v1/conversations/{conversation_id}/messages", lambda c: _messages(c),
          "Messages, chronological, cursor-paginated (after_seq)", "conversations", query=("after_seq", "limit"),
          response="page"),
        R("POST", "/v1/conversations/{conversation_id}/messages", lambda c: a(c).append_message(
            p(c), c.params["conversation_id"], content=c.body["content"],
            attachment_ids=tuple(c.body.get("attachment_ids", ()))),
          "Append an immutable user message (does not start a run)", "conversations",
          body=obj({"content": STR}, {"attachment_ids": STR_LIST})),
        # tasks
        R("GET", "/v1/tasks", _paged(lambda c, lim, off: a(c).list_tasks(p(c), status=c.query.get("status"),
                                                                          owner=c.query.get("owner"), limit=lim,
                                                                          offset=off)),
          "List tasks", "tasks", query=("status", "owner", "limit", "offset"), paginated=True, response="list"),
        R("POST", "/v1/tasks", lambda c: a(c).create_task(p(c), c.body), "Create a task (tasks:write)", "tasks",
          body=obj({"title": STR, "objective": STR, "description": STR, "owner": STR, "priority": STR,
                    "expected_output": STR, "completion_criteria": STR_LIST, "risk_level": STR},
                   {"dependencies": STR_LIST, "kpi_affected": STR_LIST, "due_date": OPT_STR, "owner_type": STR}),
          realtime=("task.created",)),
        R("PATCH", "/v1/tasks/{task_id}", lambda c: a(c).update_task(p(c), c.params["task_id"], c.body["status"],
                                                                     c.body.get("reason")),
          "Move a task through its state machine (tasks:write)", "tasks",
          body=obj({"status": STR}, {"reason": OPT_STR}), realtime=("task.updated",)),
        # outputs & evaluations
        R("GET", "/v1/outputs", _paged(lambda c, lim, off: a(c).list_outputs(p(c), agent_id=c.query.get("agent_id"),
                                                                              kind=c.query.get("kind"), limit=lim,
                                                                              offset=off)),
          "List outputs", "outputs", query=("agent_id", "kind", "limit", "offset"), paginated=True, response="list"),
        R("GET", "/v1/outputs/{output_id}", lambda c: a(c).get_output(p(c), c.params["output_id"]), "Get output",
          "outputs"),
        R("GET", "/v1/outputs/{output_id}/versions", lambda c: a(c).output_versions(p(c), c.params["output_id"]),
          "Output versions (outputs are immutable: one version)", "outputs", response="list"),
        R("GET", "/v1/outputs/{output_id}/evaluations", lambda c: a(c).list_evaluations(p(c), c.params["output_id"]),
          "Human evaluations of an output", "evaluations", response="list"),
        R("POST", "/v1/outputs/{output_id}/evaluations", lambda c: a(c).submit_output_evaluation(
            p(c), c.params["output_id"], **{k: c.body[k] for k in ("accuracy", "relevance", "completeness",
                                                                    "actionability", "business_value", "decision")},
            comment=c.body.get("comment")),
          "Submit a human evaluation (outputs:review); score and decision are independent", "evaluations",
          body=obj({"accuracy": SCORE, "relevance": SCORE, "completeness": SCORE, "actionability": SCORE,
                    "business_value": SCORE, "decision": STR}, {"comment": OPT_STR})),
        R("POST", "/v1/outputs/{output_id}/revision-requests", lambda c: a(c).request_revision(
            p(c), c.params["output_id"], c.body["comment"]),
          "Ask the producing agent to revise (creates a task)", "outputs", body=obj({"comment": STR}),
          realtime=("task.created",)),
        # approvals
        R("GET", "/v1/approvals", _paged(lambda c, lim, off: a(c).list_approvals(
            p(c), status=c.query.get("status", "PENDING"), limit=lim, offset=off)),
          "List approvals (default PENDING)", "approvals", query=("status", "limit", "offset"), paginated=True,
          response="list"),
        R("GET", "/v1/approvals/{approval_id}", lambda c: a(c).get_approval(p(c), c.params["approval_id"]),
          "Get approval (includes the exact bound payload and payload_hash)", "approvals"),
        R("POST", "/v1/approvals/{approval_id}/approve", lambda c: _approve(c),
          "Approve; payload_hash must match the bound payload you reviewed", "approvals",
          body=obj({"payload_hash": STR}, {"note": OPT_STR}), rate="approval", realtime=("approval.approved",)),
        R("POST", "/v1/approvals/{approval_id}/reject", lambda c: a(c).reject_action(
            p(c), c.params["approval_id"], c.body["reason"]),
          "Reject with a reason", "approvals", body=obj({"reason": STR}), rate="approval",
          realtime=("approval.rejected",)),
        R("POST", "/v1/approvals/{approval_id}/reconcile", lambda c: a(c).reconcile_approval(
            p(c), c.params["approval_id"], outcome=c.body["outcome"], evidence_ref=c.body["evidence_ref"],
            note=c.body["note"]),
          "Reconcile an OUTCOME_UNKNOWN execution with evidence", "approvals",
          body=obj({"outcome": STR, "evidence_ref": STR, "note": STR}), rate="approval",
          realtime=("reconciliation.completed",)),
        R("POST", "/v1/approvals/{approval_id}/reexecution", lambda c: a(c).request_reexecution(
            p(c), c.params["approval_id"]),
          "After CONFIRMED_NOT_EXECUTED: request a NEW approval for the same action", "approvals", body=obj(),
          rate="approval", realtime=("approval.requested",)),
        # recommendations & changes
        R("GET", "/v1/recommendations", _paged(lambda c, lim, off: a(c).list_recommendations(
            p(c), status=c.query.get("status"), limit=lim, offset=off)),
          "List recommendations", "recommendations", query=("status", "limit", "offset"), paginated=True,
          response="list"),
        R("GET", "/v1/recommendations/{rec_id}", lambda c: a(c).get_recommendation(p(c), c.params["rec_id"]),
          "Get recommendation", "recommendations"),
        R("POST", "/v1/recommendations/{rec_id}/approve", lambda c: a(c).approve_recommendation(
            p(c), c.params["rec_id"], c.body.get("note")),
          "Approve (recommendations:decide)", "recommendations", body=obj({}, {"note": OPT_STR}), rate="approval",
          realtime=("recommendation.updated",)),
        R("POST", "/v1/recommendations/{rec_id}/reject", lambda c: a(c).reject_recommendation(
            p(c), c.params["rec_id"], c.body["reason"]),
          "Reject with a reason (recommendations:decide)", "recommendations", body=obj({"reason": STR}),
          rate="approval", realtime=("recommendation.updated",)),
        R("POST", "/v1/recommendations/{rec_id}/change-requests", lambda c: a(c).create_change_request(
            p(c), c.params["rec_id"], c.body),
          "Convert an approved recommendation into a change request (changes:write)", "changes",
          body=obj({"current_state": STR, "problem": STR, "desired_state": STR, "change": STR, "expected_benefit": STR,
                    "risk": STR, "implementation_steps": STR_LIST, "validation_method": STR, "rollback_plan": STR,
                    "owner": STR}, {"estimated_cost": ANY_OBJ, "expected_kpi_change": {"type": "array"}})),
        R("GET", "/v1/change-requests", _paged(lambda c, lim, off: a(c).list_change_requests(
            p(c), recommendation_id=c.query.get("recommendation_id"), limit=lim, offset=off)),
          "List change requests", "changes", query=("recommendation_id", "limit", "offset"), paginated=True,
          response="list"),
        R("GET", "/v1/change-requests/{change_id}", lambda c: a(c).get_change_request(p(c), c.params["change_id"]),
          "Change request with lifecycle history and measured outcomes", "changes"),
        # KPIs
        R("GET", "/v1/kpis", lambda c: a(c).kpi_definitions(p(c)),
          "KPI definitions (version-controlled, read-only) with current value, target, variance, freshness", "kpis",
          response="list"),
        R("GET", "/v1/kpis/{kpi}/history", lambda c: a(c).get_kpi_history(p(c), c.params["kpi"],
                                                                          c.q_int("limit", 100, 500)),
          "KPI snapshot history (sourced values only)", "kpis", query=("limit",), response="list"),
        # alerts
        R("GET", "/v1/alerts", _paged(lambda c, lim, off: a(c).list_alerts(p(c), status=c.query.get("status"),
                                                                            limit=lim, offset=off)),
          "List alerts", "alerts", query=("status", "limit", "offset"), paginated=True, response="list"),
        R("POST", "/v1/alerts/{alert_id}/acknowledge", lambda c: a(c).acknowledge_alert(p(c), c.params["alert_id"]),
          "Acknowledge (alerts:write)", "alerts", body=obj()),
        R("POST", "/v1/alerts/{alert_id}/resolve", lambda c: a(c).resolve_alert(p(c), c.params["alert_id"],
                                                                               c.body["note"]),
          "Resolve with a note (alerts:write)", "alerts", body=obj({"note": STR}), realtime=("alert.resolved",)),
        # briefs
        R("GET", "/v1/briefs/daily", lambda c: _or_404(a(c).get_daily_brief(p(c), _date(c.query.get("date"), "date")),
                                                       "daily brief"),
          "Daily brief for a date", "briefs", query=("date",)),
        R("GET", "/v1/reviews/weekly", lambda c: _or_404(a(c).get_weekly_review(
            p(c), _date(c.query.get("week_start"), "week_start")), "weekly review"),
          "Weekly review for a week (Monday date)", "briefs", query=("week_start",)),
        # dashboards
        R("GET", "/v1/dashboard/executive", lambda c: a(c).executive_dashboard(p(c)),
          "Executive dashboard (one call)", "dashboards"),
        R("GET", "/v1/dashboard/agent/{agent_id}", lambda c: a(c).agent_dashboard(p(c), c.params["agent_id"]),
          "Agent dashboard (one call)", "dashboards"),
        # operations
        R("GET", "/v1/connectors/status", lambda c: _need_perm(c, "operations:read", a(c).connector_status(p(c))),
          "Connector view status (operations:read)", "operations", response="list"),
        R("GET", "/v1/readiness", lambda c: _readiness(c), "Production readiness (operations:read)", "operations"),
        # realtime
        R("GET", "/v1/events", lambda c: a(c).events_since(p(c), after=c.q_int("after", 0, 10**15),
                                                            limit=max(1, c.q_int("limit", 100, 500))),
          "Realtime events after a cursor (polling / reconnect)", "realtime", query=("after", "limit"), response="page"),
    ]


def _need_perm(c: Ctx, perm: str, value: Any) -> Any:
    if not c.principal.has(perm):
        raise PermissionDeniedError(f"missing permission {perm!r}")
    return value


def _readiness(c: Ctx) -> Any:
    if not c.principal.has("operations:read"):
        raise PermissionDeniedError("missing permission 'operations:read'")
    from .readiness import readiness
    rep = readiness()
    return {"status": rep.status.value, "checks": [{"name": x.name, "status": x.status.value, "detail": x.detail}
                                                   for x in rep.checks]}


def _or_404(value: Any, what: str) -> Any:
    if value is None:
        raise NotFoundError(f"{what} not found")
    return value


def _messages(c: Ctx) -> Any:
    return c.api.list_messages(c.principal, c.params["conversation_id"], after_seq=c.q_int("after_seq", 0, 10**12),
                               limit=max(1, c.q_int("limit", 50, 200)))


def _approve(c: Ctx) -> Any:
    approval = c.api.get_approval(c.principal, c.params["approval_id"])
    if approval["payload_hash"] != c.body["payload_hash"]:
        raise ApiError(409, "PAYLOAD_MISMATCH", "the approval's bound payload differs from the one you reviewed",
                       {"expected_payload_hash": approval["payload_hash"]})
    return c.api.approve_action(c.principal, c.params["approval_id"], c.body.get("note"))


def _command(c: Ctx) -> Any:
    from .context import ExplicitContext
    sel = c.body.get("context_selection") or []
    items = []
    for i, item in enumerate(sel):
        if not isinstance(item, dict) or set(item) - {"item_id", "title", "text"} or not item.get("text"):
            raise ApiError(422, "VALIDATION_FAILED", f"context_selection[{i}] needs item_id, title, text")
        items.append(ExplicitContext(str(item.get("item_id") or i), str(item.get("title") or "context"),
                                     str(item["text"])[:20000]))
    receipt = c.api.create_command(c.principal, agent_id=c.body["requested_agent_id"], message=c.body["message"],
                                   attachments=tuple(c.body.get("attachment_ids") or ()),
                                   context_selection=tuple(items), conversation_id=c.body.get("conversation_id"),
                                   idempotency_key=c.body.get("idempotency_key"))
    queue = c.api.commands.queue
    job = None
    if queue is not None:
        key = f"command:{c.body.get('idempotency_key') or receipt['run_id']}"
        job = queue.find_by_key(c.principal.organization_id, key)
    return {"command_id": job.job_id if job else receipt["run_id"], "run_id": receipt["run_id"],
            "conversation_id": receipt["conversation_id"], "status": receipt["status"]}


# ---------------------------------------------------------------------------------------
# the application
# ---------------------------------------------------------------------------------------
@dataclass
class HttpApi:
    api: ApiService
    verifier: TokenVerifier | None
    directory: MembershipDirectory
    limiter: RateLimiter
    health: Any = None                       # callable() -> (ready: bool, checks: dict)
    production: bool = False
    routes: list[Route] = field(default_factory=build_routes)

    def _principal(self, req: HttpRequest) -> Principal:
        auth = req.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise AuthenticationError("missing bearer token")
        if self.verifier is None:
            raise AuthenticationError("authentication is not configured")
        identity = self.verifier.verify(auth[7:].strip())
        served = set(self.api.tenants.tenants)
        return resolve_principal(identity, self.directory, req.headers.get("x-organization") or None, served)

    def _limits(self, route: Route, principal: Principal) -> None:
        if route.rate == "none":
            return
        cc = self.api.tenants.for_principal(principal)
        check_limits(self.limiter, cc.policy.rate_limits, route.rate, user=principal.principal_id,
                     org=principal.organization_id)

    def match(self, method: str, path: str) -> tuple[Route | None, dict, bool]:
        path_matched = False
        for r in self.routes:
            m = r.pattern.match(path)
            if m:
                path_matched = True
                if r.method == method:
                    return r, m.groupdict(), True
        return None, {}, path_matched

    def handle(self, req: HttpRequest) -> HttpResponse:
        rid = req.headers.get("x-request-id") or new_id("req")
        meta = {"request_id": rid}
        started = time.monotonic()
        status, user, org = 500, None, None
        try:
            if req.path == "/health/live":
                status = 200
                return HttpResponse(200, envelope({"status": "live"}, meta))
            if req.path == "/health/ready":
                ready, checks = self.health() if self.health else (True, {})
                status = 200 if ready else 503
                return HttpResponse(status, envelope({"status": "ready" if ready else "not_ready", "checks": checks},
                                                     meta))
            if req.path == "/v1/openapi.json":
                from .openapi import openapi_document
                status = 200
                return HttpResponse(200, openapi_document(self.routes))
            route, params, path_matched = self.match(req.method, req.path)
            if route is None:
                raise ApiError(405 if path_matched else 404, "METHOD_NOT_ALLOWED" if path_matched else "NOT_FOUND",
                               "method not allowed" if path_matched else "no such route")
            principal = self._principal(req)
            user, org = principal.principal_id, principal.organization_id
            self._limits(route, principal)
            body: dict = {}
            if route.body is not None:
                try:
                    body = json.loads(req.body or b"{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise ApiError(422, "VALIDATION_FAILED", "request body is not valid JSON") from None
                errors = validate_body(route.body, body)
                if errors:
                    code = "CLIENT_SUPPLIED_AUTHORITY" if any("may not be supplied" in e for e in errors) \
                        else "VALIDATION_FAILED"
                    raise ApiError(422, code, "invalid request", {"errors": errors})
            elif req.body and req.body.strip() and req.method == "GET":
                raise ApiError(422, "VALIDATION_FAILED", "GET requests take no body")
            result = route.handler(Ctx(principal, self.api, params, req.query, body, rid))
            if isinstance(result, tuple):
                data, extra = result
                meta.update(extra)
            else:
                data = result
            status = 201 if req.method == "POST" and route.path in ("/v1/commands", "/v1/conversations",
                                                                     "/v1/tasks") else 200
            return HttpResponse(status, envelope(to_json(data), meta))
        except Exception as exc:
            err = map_exception(exc)
            status = err.status
            if status == 500:
                log.exception("unhandled error", extra={"request_id": rid})
            return HttpResponse(status, envelope(None, meta, err), dict(err.headers))
        finally:
            log.info("request", extra={"request_id": rid, "method": req.method, "path": req.path, "status": status,
                                       "user_id": user, "organization_id": org,
                                       "latency_ms": round((time.monotonic() - started) * 1000, 1)})


def json_bytes(body: Any) -> bytes:
    def default(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        return str(v)
    return json.dumps(body, default=default, separators=(",", ":")).encode()
