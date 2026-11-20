# Frontend API contract (Lovable)

Generated from `command_center.http_api.build_routes()`; do not edit by hand. Regenerate with
`python -c "from command_center.openapi import write_docs; write_docs()"`. Machine-readable form:
`docs/openapi.json` (also served at `GET /v1/openapi.json`).

## Authentication

- Send the Supabase Auth access token: `Authorization: Bearer <access_token>` (from `supabase.auth.getSession()`).
- The server verifies the token (signature, issuer `<SUPABASE_URL>/auth/v1`, audience `authenticated`, expiry).
- Organization, roles and permissions are loaded **server-side** from membership tables. Never send
  `organization_id`, `roles`, `permissions`, `user_id` or approver fields: requests containing them are rejected
  with `422 CLIENT_SUPPLIED_AUTHORITY`. JWT custom claims are ignored for authorization.
- Users in several organizations select one with `X-Organization: <organization-slug>`; the server checks
  membership (`403` otherwise). Without it, a multi-organization user gets `422 ORGANIZATION_SELECTION_REQUIRED`.
- `GET /v1/me` returns the server-resolved identity; use it to show/hide actions (the server still enforces).

## Envelope

Every response: `{"data": ..., "meta": {"request_id": ...}, "error": null}`. Failures:
`{"data": null, "meta": {...}, "error": {"code", "message", "details"}}`. No stack traces are returned.

| Status | Codes | Meaning |
|---|---|---|
| 401 | `UNAUTHENTICATED` | Missing, malformed, expired or untrusted token: refresh the session |
| 403 | `FORBIDDEN` | Not a member, missing permission, or missing approver role |
| 404 | `NOT_FOUND` | Not found (records of other organizations are always 404) |
| 405 | `METHOD_NOT_ALLOWED` | Route exists with another method |
| 409 | `CONFLICT`, `PAYLOAD_MISMATCH` | Invalid state transition, already decided, or approval payload changed |
| 422 | `VALIDATION_FAILED`, `CLIENT_SUPPLIED_AUTHORITY`, `ORGANIZATION_SELECTION_REQUIRED` | Bad input |
| 429 | `RATE_LIMITED`, `BUDGET_EXCEEDED` | Back off; honour `Retry-After` |
| 500 | `INTERNAL_ERROR` | Unexpected error; quote `meta.request_id` to support |

## Pagination

- Offset lists: `?limit=` (1-200, default 50) and `?offset=`; `meta.pagination.next_offset` is null on the last page.
- Cursor lists (messages, events): `data.items` + `data.next_cursor`; pass it back as `after_seq` / `after`.

## Realtime

- Stream: `GET /v1/events/stream` (Server-Sent Events). Use a fetch-based EventSource client so the
  `Authorization` header can be sent (tokens in query strings are rejected). Resume with `Last-Event-ID`;
  connections close after ~25 s by design: reconnect with the last id.
- Polling fallback: `GET /v1/events?after=<cursor>`.
- Each event: `{id, event_type, timestamp, object_type, object_id, payload_summary}`. Summaries are
  allow-listed (status, titles, ids); fetch the object through its endpoint for details.

| Event | Meaning |
|---|---|
| `run.queued` | A run was created (command accepted) |
| `run.started` | A run started |
| `run.progress` | A model step completed |
| `run.tool_requested` | The agent requested a tool |
| `tool.completed` | A tool call succeeded |
| `tool.failed` | A tool call failed |
| `run.waiting_approval` | The run is paused for approval |
| `run.resumed` | The run resumed |
| `run.completed` | A run succeeded |
| `run.failed` | A run failed |
| `run.cancelled` | A run was cancelled |
| `approval.requested` | Approval required (AMBER/RED action) |
| `approval.approved` | Approved |
| `approval.rejected` | Rejected |
| `reconciliation.required` | External outcome unknown: reconcile |
| `reconciliation.completed` | Reconciled with evidence |
| `reconciliation.unresolved` | Reconciliation pending |
| `output.created` | An agent output was stored |
| `task.created` | Task created |
| `task.updated` | Task changed |
| `recommendation.created` | Recommendation proposed |
| `recommendation.updated` | Recommendation decided/changed |
| `alert.created` | Alert raised |
| `alert.acknowledged` | Alert acknowledged |
| `alert.resolved` | Alert resolved |
| `message.created` | Conversation message stored |
| `workflow.started` | Workflow started |
| `workflow.completed` | Workflow succeeded |
| `workflow.failed` | Workflow failed |

## Rate limits

Per user and organization (organization policy `rate_limits`): commands per user per minute, commands and runs
per organization per hour, approval decisions per user per minute, API requests per user per minute. Budgets
still apply to every run (`429 BUDGET_EXCEEDED`).

## Endpoints

### identity

#### `GET /v1/me`

Current user, organization, roles and permissions (server-resolved)

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### agents

#### `GET /v1/agents`

List agents

- Authentication: Bearer token
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/agents/{agent_id}`

Get agent

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/agents/{agent_id}/performance`

Agent performance (7d/30d/90d); needs performance:read

- Authentication: Bearer token
- Query: `period`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### runs

#### `GET /v1/runs`

List runs

- Authentication: Bearer token
- Query: `agent_id`, `status`, `limit`, `offset`
- Pagination: offset (`limit`, `offset`, `meta.pagination.next_offset`)
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/runs/{run_id}`

Get run

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/runs/{run_id}/cancel`

Cancel a run (initiator or runs:cancel)

- Authentication: Bearer token
- Realtime: `run.cancelled`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "reason": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "reason"
  ],
  "type": "object"
}
```

### commands

#### `POST /v1/commands`

Submit a command (async; returns command/run ids)

- Authentication: Bearer token
- Rate-limit category: `command`
- Realtime: `run.queued`, `run.started`, `run.completed`, `run.failed`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "attachment_ids": {
      "items": {
        "type": "string"
      },
      "type": "array"
    },
    "context_selection": {
      "type": "array"
    },
    "conversation_id": {
      "type": [
        "string",
        "null"
      ]
    },
    "idempotency_key": {
      "type": [
        "string",
        "null"
      ]
    },
    "message": {
      "minLength": 1,
      "type": "string"
    },
    "requested_agent_id": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "message",
    "requested_agent_id"
  ],
  "type": "object"
}
```

#### `GET /v1/commands/{command_id}`

Command status

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### conversations

#### `GET /v1/conversations`

List conversations (most recent first)

- Authentication: Bearer token
- Query: `limit`
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/conversations`

Create a conversation

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "primary_agent_id": {
      "type": [
        "string",
        "null"
      ]
    },
    "title": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "title"
  ],
  "type": "object"
}
```

#### `GET /v1/conversations/{conversation_id}/messages`

Messages, chronological, cursor-paginated (after_seq)

- Authentication: Bearer token
- Query: `after_seq`, `limit`
- Pagination: cursor (`data.next_cursor`)
- Response `data`: page object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/conversations/{conversation_id}/messages`

Append an immutable user message (does not start a run)

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "attachment_ids": {
      "items": {
        "type": "string"
      },
      "type": "array"
    },
    "content": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "content"
  ],
  "type": "object"
}
```

### tasks

#### `GET /v1/tasks`

List tasks

- Authentication: Bearer token
- Query: `status`, `owner`, `limit`, `offset`
- Pagination: offset (`limit`, `offset`, `meta.pagination.next_offset`)
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/tasks`

Create a task (tasks:write)

- Authentication: Bearer token
- Realtime: `task.created`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "completion_criteria": {
      "items": {
        "type": "string"
      },
      "type": "array"
    },
    "dependencies": {
      "items": {
        "type": "string"
      },
      "type": "array"
    },
    "description": {
      "minLength": 1,
      "type": "string"
    },
    "due_date": {
      "type": [
        "string",
        "null"
      ]
    },
    "expected_output": {
      "minLength": 1,
      "type": "string"
    },
    "kpi_affected": {
      "items": {
        "type": "string"
      },
      "type": "array"
    },
    "objective": {
      "minLength": 1,
      "type": "string"
    },
    "owner": {
      "minLength": 1,
      "type": "string"
    },
    "owner_type": {
      "minLength": 1,
      "type": "string"
    },
    "priority": {
      "minLength": 1,
      "type": "string"
    },
    "risk_level": {
      "minLength": 1,
      "type": "string"
    },
    "title": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "completion_criteria",
    "description",
    "expected_output",
    "objective",
    "owner",
    "priority",
    "risk_level",
    "title"
  ],
  "type": "object"
}
```

#### `PATCH /v1/tasks/{task_id}`

Move a task through its state machine (tasks:write)

- Authentication: Bearer token
- Realtime: `task.updated`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "reason": {
      "type": [
        "string",
        "null"
      ]
    },
    "status": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "status"
  ],
  "type": "object"
}
```

### outputs

#### `GET /v1/outputs`

List outputs

- Authentication: Bearer token
- Query: `agent_id`, `kind`, `limit`, `offset`
- Pagination: offset (`limit`, `offset`, `meta.pagination.next_offset`)
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/outputs/{output_id}`

Get output

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/outputs/{output_id}/versions`

Output versions (outputs are immutable: one version)

- Authentication: Bearer token
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/outputs/{output_id}/revision-requests`

Ask the producing agent to revise (creates a task)

- Authentication: Bearer token
- Realtime: `task.created`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "comment": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "comment"
  ],
  "type": "object"
}
```

### evaluations

#### `GET /v1/outputs/{output_id}/evaluations`

Human evaluations of an output

- Authentication: Bearer token
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/outputs/{output_id}/evaluations`

Submit a human evaluation (outputs:review); score and decision are independent

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "accuracy": {
      "maximum": 5,
      "minimum": 1,
      "type": "integer"
    },
    "actionability": {
      "maximum": 5,
      "minimum": 1,
      "type": "integer"
    },
    "business_value": {
      "maximum": 5,
      "minimum": 1,
      "type": "integer"
    },
    "comment": {
      "type": [
        "string",
        "null"
      ]
    },
    "completeness": {
      "maximum": 5,
      "minimum": 1,
      "type": "integer"
    },
    "decision": {
      "minLength": 1,
      "type": "string"
    },
    "relevance": {
      "maximum": 5,
      "minimum": 1,
      "type": "integer"
    }
  },
  "required": [
    "accuracy",
    "actionability",
    "business_value",
    "completeness",
    "decision",
    "relevance"
  ],
  "type": "object"
}
```

### approvals

#### `GET /v1/approvals`

List approvals (default PENDING)

- Authentication: Bearer token
- Query: `status`, `limit`, `offset`
- Pagination: offset (`limit`, `offset`, `meta.pagination.next_offset`)
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/approvals/{approval_id}`

Get approval (includes the exact bound payload and payload_hash)

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/approvals/{approval_id}/approve`

Approve; payload_hash must match the bound payload you reviewed

- Authentication: Bearer token
- Rate-limit category: `approval`
- Realtime: `approval.approved`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "note": {
      "type": [
        "string",
        "null"
      ]
    },
    "payload_hash": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "payload_hash"
  ],
  "type": "object"
}
```

#### `POST /v1/approvals/{approval_id}/reject`

Reject with a reason

- Authentication: Bearer token
- Rate-limit category: `approval`
- Realtime: `approval.rejected`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "reason": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "reason"
  ],
  "type": "object"
}
```

#### `POST /v1/approvals/{approval_id}/reconcile`

Reconcile an OUTCOME_UNKNOWN execution with evidence

- Authentication: Bearer token
- Rate-limit category: `approval`
- Realtime: `reconciliation.completed`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "evidence_ref": {
      "minLength": 1,
      "type": "string"
    },
    "note": {
      "minLength": 1,
      "type": "string"
    },
    "outcome": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "evidence_ref",
    "note",
    "outcome"
  ],
  "type": "object"
}
```

#### `POST /v1/approvals/{approval_id}/reexecution`

After CONFIRMED_NOT_EXECUTED: request a NEW approval for the same action

- Authentication: Bearer token
- Rate-limit category: `approval`
- Realtime: `approval.requested`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {},
  "required": [],
  "type": "object"
}
```

### recommendations

#### `GET /v1/recommendations`

List recommendations

- Authentication: Bearer token
- Query: `status`, `limit`, `offset`
- Pagination: offset (`limit`, `offset`, `meta.pagination.next_offset`)
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/recommendations/{rec_id}`

Get recommendation

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/recommendations/{rec_id}/approve`

Approve (recommendations:decide)

- Authentication: Bearer token
- Rate-limit category: `approval`
- Realtime: `recommendation.updated`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "note": {
      "type": [
        "string",
        "null"
      ]
    }
  },
  "required": [],
  "type": "object"
}
```

#### `POST /v1/recommendations/{rec_id}/reject`

Reject with a reason (recommendations:decide)

- Authentication: Bearer token
- Rate-limit category: `approval`
- Realtime: `recommendation.updated`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "reason": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "reason"
  ],
  "type": "object"
}
```

### changes

#### `POST /v1/recommendations/{rec_id}/change-requests`

Convert an approved recommendation into a change request (changes:write)

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "change": {
      "minLength": 1,
      "type": "string"
    },
    "current_state": {
      "minLength": 1,
      "type": "string"
    },
    "desired_state": {
      "minLength": 1,
      "type": "string"
    },
    "estimated_cost": {
      "type": "object"
    },
    "expected_benefit": {
      "minLength": 1,
      "type": "string"
    },
    "expected_kpi_change": {
      "type": "array"
    },
    "implementation_steps": {
      "items": {
        "type": "string"
      },
      "type": "array"
    },
    "owner": {
      "minLength": 1,
      "type": "string"
    },
    "problem": {
      "minLength": 1,
      "type": "string"
    },
    "risk": {
      "minLength": 1,
      "type": "string"
    },
    "rollback_plan": {
      "minLength": 1,
      "type": "string"
    },
    "validation_method": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "change",
    "current_state",
    "desired_state",
    "expected_benefit",
    "implementation_steps",
    "owner",
    "problem",
    "risk",
    "rollback_plan",
    "validation_method"
  ],
  "type": "object"
}
```

#### `GET /v1/change-requests`

List change requests

- Authentication: Bearer token
- Query: `recommendation_id`, `limit`, `offset`
- Pagination: offset (`limit`, `offset`, `meta.pagination.next_offset`)
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/change-requests/{change_id}`

Change request with lifecycle history and measured outcomes

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### kpis

#### `GET /v1/kpis`

KPI definitions (version-controlled, read-only) with current value, target, variance, freshness

- Authentication: Bearer token
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/kpis/{kpi}/history`

KPI snapshot history (sourced values only)

- Authentication: Bearer token
- Query: `limit`
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### alerts

#### `GET /v1/alerts`

List alerts

- Authentication: Bearer token
- Query: `status`, `limit`, `offset`
- Pagination: offset (`limit`, `offset`, `meta.pagination.next_offset`)
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `POST /v1/alerts/{alert_id}/acknowledge`

Acknowledge (alerts:write)

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {},
  "required": [],
  "type": "object"
}
```

#### `POST /v1/alerts/{alert_id}/resolve`

Resolve with a note (alerts:write)

- Authentication: Bearer token
- Realtime: `alert.resolved`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

Request body:

```json
{
  "additionalProperties": false,
  "properties": {
    "note": {
      "minLength": 1,
      "type": "string"
    }
  },
  "required": [
    "note"
  ],
  "type": "object"
}
```

### briefs

#### `GET /v1/briefs/daily`

Daily brief for a date

- Authentication: Bearer token
- Query: `date`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/reviews/weekly`

Weekly review for a week (Monday date)

- Authentication: Bearer token
- Query: `week_start`
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### dashboards

#### `GET /v1/dashboard/executive`

Executive dashboard (one call)

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/dashboard/agent/{agent_id}`

Agent dashboard (one call)

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### operations

#### `GET /v1/connectors/status`

Connector view status (operations:read)

- Authentication: Bearer token
- Response `data`: array
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

#### `GET /v1/readiness`

Production readiness (operations:read)

- Authentication: Bearer token
- Response `data`: object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### realtime

#### `GET /v1/events`

Realtime events after a cursor (polling / reconnect)

- Authentication: Bearer token
- Query: `after`, `limit`
- Pagination: cursor (`data.next_cursor`)
- Response `data`: page object
- Errors: 401, 403, 404, 409, 422, 429, 500 (envelope above)

### health

#### `GET /health/live`

Liveness (no authentication).

#### `GET /health/ready`

Readiness: database, configuration, queue, migrations, model provider (production). 200 when ready, 503 otherwise. No authentication.

#### `GET /v1/events/stream`

Server-Sent Events stream (see Realtime).

