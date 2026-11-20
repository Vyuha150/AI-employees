"""OpenAPI 3.1 document generated from the HTTP route table (single source of truth)."""

from __future__ import annotations

import re
from typing import Any

ENVELOPE = {
    "type": "object", "required": ["data", "meta", "error"],
    "properties": {"data": {}, "meta": {"type": "object", "properties": {
        "request_id": {"type": "string"},
        "pagination": {"type": "object", "properties": {"limit": {"type": "integer"}, "offset": {"type": "integer"},
                                                         "next_offset": {"type": ["integer", "null"]}}}}},
        "error": {"oneOf": [{"type": "null"}, {"$ref": "#/components/schemas/Error"}]}},
}
ERROR = {"type": "object", "required": ["code", "message"],
         "properties": {"code": {"type": "string"}, "message": {"type": "string"}, "details": {}}}
ERROR_RESPONSES = {
    "401": "Missing, invalid or expired token", "403": "Not a member, or missing permission/role",
    "404": "Not found (including other organizations' records)", "409": "State conflict or payload mismatch",
    "422": "Validation failed (including client-supplied organization/role/permission fields)",
    "429": "Rate limited or budget exceeded (see Retry-After)", "500": "Internal error (no details exposed)",
}


def openapi_document(routes: list[Any]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for r in routes:
        params = [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}}
                  for n in re.findall(r"\{(\w+)\}", r.path)]
        params += [{"name": q, "in": "query", "required": False, "schema": {"type": "string"}} for q in r.query]
        op: dict[str, Any] = {
            "summary": r.summary, "tags": [r.tag], "operationId": f"{r.method.lower()}_{re.sub(r'[^a-z0-9]+', '_', r.path.lower()).strip('_')}",
            "parameters": params,
            "responses": {"200" if not (r.method == "POST" and r.path in ("/v1/commands", "/v1/conversations", "/v1/tasks"))
                          else "201": {"description": "Success", "content": {"application/json": {
                              "schema": {"$ref": "#/components/schemas/Envelope"}}}},
                          **{code: {"description": d, "content": {"application/json": {
                              "schema": {"$ref": "#/components/schemas/Envelope"}}}}
                             for code, d in ERROR_RESPONSES.items()}},
        }
        if r.auth:
            op["security"] = [{"supabaseJwt": []}]
        if r.body is not None:
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": r.body}}}
        ext = {}
        if r.paginated:
            ext["x-pagination"] = "offset (limit, offset; meta.pagination.next_offset)"
        if r.response == "page":
            ext["x-pagination"] = "cursor (see data.next_cursor)"
        if r.realtime:
            ext["x-realtime-events"] = list(r.realtime)
        if r.rate != "api":
            ext["x-rate-limit-category"] = r.rate
        op.update(ext)
        paths.setdefault(r.path, {})[r.method.lower()] = op
    paths["/v1/events/stream"] = {"get": {
        "summary": "Server-Sent Events stream of organization realtime events (Last-Event-ID or ?after= to resume)",
        "tags": ["realtime"], "security": [{"supabaseJwt": []}],
        "parameters": [{"name": "after", "in": "query", "schema": {"type": "string"}}],
        "responses": {"200": {"description": "text/event-stream"}, "401": {"description": ERROR_RESPONSES["401"]}}}}
    for path, d in (("/health/live", "Process is alive"), ("/health/ready", "Ready (DB, config, queue, migrations, model)")):
        paths[path] = {"get": {"summary": d, "tags": ["health"], "responses": {
            "200": {"description": "OK"}, "503": {"description": "Not ready"}}}}
    return {
        "openapi": "3.1.0",
        "info": {"title": "AI Business Command Center API", "version": "1.0.0",
                 "description": "Authenticated with Supabase access tokens. Organization, roles and permissions are "
                                "resolved server-side; never send them. All responses use the {data, meta, error} "
                                "envelope."},
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {
            "securitySchemes": {"supabaseJwt": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                                                "description": "Supabase Auth access token. Optional header "
                                                               "X-Organization: <slug> selects one of the user's "
                                                               "memberships."}},
            "schemas": {"Envelope": ENVELOPE, "Error": ERROR},
        },
    }


REALTIME_EVENTS = [
    ("run.queued", "A run was created (command accepted)"), ("run.started", "A run started"),
    ("run.progress", "A model step completed"), ("run.tool_requested", "The agent requested a tool"),
    ("tool.completed", "A tool call succeeded"), ("tool.failed", "A tool call failed"),
    ("run.waiting_approval", "The run is paused for approval"), ("run.resumed", "The run resumed"),
    ("run.completed", "A run succeeded"), ("run.failed", "A run failed"), ("run.cancelled", "A run was cancelled"),
    ("approval.requested", "Approval required (AMBER/RED action)"), ("approval.approved", "Approved"),
    ("approval.rejected", "Rejected"), ("reconciliation.required", "External outcome unknown: reconcile"),
    ("reconciliation.completed", "Reconciled with evidence"), ("reconciliation.unresolved", "Reconciliation pending"),
    ("output.created", "An agent output was stored"), ("task.created", "Task created"), ("task.updated", "Task changed"),
    ("recommendation.created", "Recommendation proposed"), ("recommendation.updated", "Recommendation decided/changed"),
    ("alert.created", "Alert raised"), ("alert.acknowledged", "Alert acknowledged"), ("alert.resolved", "Alert resolved"),
    ("message.created", "Conversation message stored"),
    ("workflow.started", "Workflow started"), ("workflow.completed", "Workflow succeeded"),
    ("workflow.failed", "Workflow failed"),
]


def frontend_markdown(routes: list[Any]) -> str:
    """Human-readable API contract for the frontend, generated from the same route table."""
    import json
    lines = [
        "# Frontend API contract (Lovable)",
        "",
        "Generated from `command_center.http_api.build_routes()`; do not edit by hand. Regenerate with",
        "`python -c \"from command_center.openapi import write_docs; write_docs()\"`. Machine-readable form:",
        "`docs/openapi.json` (also served at `GET /v1/openapi.json`).",
        "",
        "## Authentication",
        "",
        "- Send the Supabase Auth access token: `Authorization: Bearer <access_token>` (from `supabase.auth.getSession()`).",
        "- The server verifies the token (signature, issuer `<SUPABASE_URL>/auth/v1`, audience `authenticated`, expiry).",
        "- Organization, roles and permissions are loaded **server-side** from membership tables. Never send",
        "  `organization_id`, `roles`, `permissions`, `user_id` or approver fields: requests containing them are rejected",
        "  with `422 CLIENT_SUPPLIED_AUTHORITY`. JWT custom claims are ignored for authorization.",
        "- Users in several organizations select one with `X-Organization: <organization-slug>`; the server checks",
        "  membership (`403` otherwise). Without it, a multi-organization user gets `422 ORGANIZATION_SELECTION_REQUIRED`.",
        "- `GET /v1/me` returns the server-resolved identity; use it to show/hide actions (the server still enforces).",
        "",
        "## Envelope",
        "",
        "Every response: `{\"data\": ..., \"meta\": {\"request_id\": ...}, \"error\": null}`. Failures:",
        "`{\"data\": null, \"meta\": {...}, \"error\": {\"code\", \"message\", \"details\"}}`. No stack traces are returned.",
        "",
        "| Status | Codes | Meaning |",
        "|---|---|---|",
        "| 401 | `UNAUTHENTICATED` | Missing, malformed, expired or untrusted token: refresh the session |",
        "| 403 | `FORBIDDEN` | Not a member, missing permission, or missing approver role |",
        "| 404 | `NOT_FOUND` | Not found (records of other organizations are always 404) |",
        "| 405 | `METHOD_NOT_ALLOWED` | Route exists with another method |",
        "| 409 | `CONFLICT`, `PAYLOAD_MISMATCH` | Invalid state transition, already decided, or approval payload changed |",
        "| 422 | `VALIDATION_FAILED`, `CLIENT_SUPPLIED_AUTHORITY`, `ORGANIZATION_SELECTION_REQUIRED` | Bad input |",
        "| 429 | `RATE_LIMITED`, `BUDGET_EXCEEDED` | Back off; honour `Retry-After` |",
        "| 500 | `INTERNAL_ERROR` | Unexpected error; quote `meta.request_id` to support |",
        "",
        "## Pagination",
        "",
        "- Offset lists: `?limit=` (1-200, default 50) and `?offset=`; `meta.pagination.next_offset` is null on the last page.",
        "- Cursor lists (messages, events): `data.items` + `data.next_cursor`; pass it back as `after_seq` / `after`.",
        "",
        "## Realtime",
        "",
        "- Stream: `GET /v1/events/stream` (Server-Sent Events). Use a fetch-based EventSource client so the",
        "  `Authorization` header can be sent (tokens in query strings are rejected). Resume with `Last-Event-ID`;",
        "  connections close after ~25 s by design: reconnect with the last id.",
        "- Polling fallback: `GET /v1/events?after=<cursor>`.",
        "- Each event: `{id, event_type, timestamp, object_type, object_id, payload_summary}`. Summaries are",
        "  allow-listed (status, titles, ids); fetch the object through its endpoint for details.",
        "",
        "| Event | Meaning |",
        "|---|---|",
        *[f"| `{n}` | {d} |" for n, d in REALTIME_EVENTS],
        "",
        "## Rate limits",
        "",
        "Per user and organization (organization policy `rate_limits`): commands per user per minute, commands and runs",
        "per organization per hour, approval decisions per user per minute, API requests per user per minute. Budgets",
        "still apply to every run (`429 BUDGET_EXCEEDED`).",
        "",
        "## Endpoints",
        "",
    ]
    by_tag: dict[str, list[Any]] = {}
    for r in routes:
        by_tag.setdefault(r.tag, []).append(r)
    for tag, rs in by_tag.items():
        lines += [f"### {tag}", ""]
        for r in rs:
            lines += [f"#### `{r.method} {r.path}`", "", r.summary, ""]
            lines.append(f"- Authentication: {'Bearer token' if r.auth else 'none'}")
            if r.query:
                lines.append(f"- Query: {', '.join('`' + q + '`' for q in r.query)}")
            if r.paginated:
                lines.append("- Pagination: offset (`limit`, `offset`, `meta.pagination.next_offset`)")
            elif r.response == "page":
                lines.append("- Pagination: cursor (`data.next_cursor`)")
            if r.rate != "api":
                lines.append(f"- Rate-limit category: `{r.rate}`")
            if r.realtime:
                lines.append(f"- Realtime: {', '.join('`' + e + '`' for e in r.realtime)}")
            lines.append(f"- Response `data`: {'array' if r.response == 'list' else ('page object' if r.response == 'page' else 'object')}")
            lines.append("- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)")
            if r.body is not None:
                lines += ["", "Request body:", "", "```json", json.dumps(r.body, indent=2, sort_keys=True), "```"]
            lines.append("")
    lines += ["### health", "", "#### `GET /health/live`", "", "Liveness (no authentication).", "",
              "#### `GET /health/ready`", "", "Readiness: database, configuration, queue, migrations, model provider "
              "(production). 200 when ready, 503 otherwise. No authentication.", "",
              "#### `GET /v1/events/stream`", "", "Server-Sent Events stream (see Realtime).", ""]
    return "\n".join(lines) + "\n"


def write_docs(root: Any = None) -> None:
    import json
    from pathlib import Path
    from .http_api import build_routes
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    routes = build_routes()
    (root / "docs" / "openapi.json").write_text(json.dumps(openapi_document(routes), indent=2, sort_keys=True) + "\n")
    (root / "docs" / "frontend_api.md").write_text(frontend_markdown(routes))
