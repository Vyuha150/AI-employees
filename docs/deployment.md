# Deployment: API, worker, scheduler and Supabase

> **Status.** This setup has **not** been run against a real Supabase project. No Supabase project or
> credentials were available while it was built. JWT verification, memberships, rate limits, queue
> durability, leader election and the RLS policies were tested against a local PostgreSQL 16 server.
> For RLS, that server used a stub `auth.uid()` that reads `request.jwt.claim.sub`, as Supabase does.
> Steps 1–9 below are what you still need to do. Do not treat them as done until you have checked each
> one in your own project.

## Processes

| Command | Role | Health |
|---|---|---|
| `ai-command-center serve [--host H --port P]` | HTTP API with SSE realtime (uvicorn). Stateless; run as many as you need. | `GET /health/live`, `GET /health/ready` on the API port |
| `ai-command-center worker [--health-port P]` | Claims durable queue jobs (agent runs, scheduled workflows). Run one or more. | `--health-port` serves the same two endpoints |
| `ai-command-center scheduler [--health-port P] [--tick-seconds 30]` | Triggers scheduled workflows. Run two for failover: only the one holding the PostgreSQL advisory lock acts. | as above |
| `ai-command-center migrate` | Applies `0001_core.sql`, `0002_operations.sql` and `0003_api.sql` (tracked in `schema_migrations`). | — |

- **Shutdown.** SIGTERM or SIGINT stops a worker after its current job. Unclaimed jobs stay `QUEUED`. If a
  process dies mid-job, the job's lease expires and `reclaim_abandoned` applies the reclaim rules: a run
  that never started is re-queued, and an uncertain side effect is never retried automatically. uvicorn
  drains requests for up to 20 seconds.
- **Scheduler leadership.** The scheduler holds a session-level `pg_try_advisory_lock` on a dedicated
  connection. If the leader dies, PostgreSQL releases the lock and the standby takes over on its next tick.
  Only one scheduler is ever leader. Occurrences are idempotent per (job, scheduled time), so a takeover
  cannot double-trigger a workflow.
- **Scheduled agent workflows.** The scheduler enqueues these as `scheduled_workflow` jobs, and a worker
  runs them. Escalation evaluation runs inline because it uses no model.
- **Logs.** All processes write JSON lines to stdout. Tokens, API keys and database URLs are masked, and
  tracebacks are never emitted (only `error_type`).
- **Readiness.** `/health/ready` checks config, database connectivity, migrations being current, the queue
  and, in production, the model provider. Optional connectors are not part of readiness; use
  `GET /v1/connectors/status` or `ai-command-center connectors status` for those.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `CC_ENVIRONMENT` | yes | `production` enforces database, auth, provider and the placeholder review |
| `CC_CONFIG_DIR` | production | Your reviewed copy of the packaged config |
| `CC_DATABASE_URL` | production | Server-side PostgreSQL DSN (Supabase: the pooled or direct connection string) |
| `CC_SUPABASE_URL` | production | `https://<ref>.supabase.co`. Issuer is `<url>/auth/v1`; JWKS is `<url>/auth/v1/.well-known/jwks.json` |
| `CC_SUPABASE_JWT_SECRET` | if HS256 | The legacy JWT secret. Leave it unset if your project uses asymmetric signing keys (ES256/RS256 via JWKS) |
| `CC_SUPABASE_JWKS_URL` | optional | Overrides the JWKS URL |
| `CC_JWT_AUDIENCE` | optional | Defaults to `authenticated` |
| `CC_CORS_ORIGINS` | yes, for a browser | Comma-separated exact origins, such as the Lovable preview and production URLs |
| `CC_SCHEDULER_USER_ID` | for scheduled agent workflows | The user id of a service-account member; its roles come from membership |
| `ANTHROPIC_API_KEY` | production | Model provider key (server only) |
| `CC_API_HOST`, `CC_API_PORT`, `CC_LOG_LEVEL` | optional | Bind address (`0.0.0.0:8080`) and log level |

The backend never needs the Supabase **service-role key**, and the frontend must never be given one.

## Supabase steps (not yet performed)

1. **Create the project.** Note the project ref and the database connection string. Under
   Auth → JWT Keys, see whether the project signs with the legacy HS256 secret or with asymmetric keys.
2. **Apply the migrations** from a trusted machine:
   `CC_DATABASE_URL='postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres' ai-command-center migrate`
3. **Apply RLS** after the three core migrations:
   `psql "$CC_DATABASE_URL" -f src/command_center/resources/migrations/supabase/0101_rls.sql`
   (on Supabase, `auth.uid()` already exists, so do not create the test stub).
4. **Verify RLS in the project.** Check two things as an `authenticated` user (SQL editor, "Run as" a user):
   that user sees only their organization's tasks; and without `audit:read`, `SELECT` on `audit_events`
   returns 0 rows. Confirm `jobs`, `rate_limit_counters` and `process_heartbeats` are not readable by
   `authenticated`. Record the results; nothing here has been verified in Supabase.
5. **Create the organization row.** The API does this on its first start (`ensure_organization` uses
   `organization_id` from your policy file).
6. **Provision memberships** through the privileged backend path, never from the client. For example:
   ```python
   from command_center.pg.database import Database
   from command_center.pg.membership import add_member
   db = Database(DSN)
   add_member(db, ORG_UUID, "<auth.users.id>", {"FOUNDER": ["run_agent:cfo", "tasks:write", ...]},
              display_name="Founder", granted_by="<who approved this>")
   ```
   Wildcard permissions are refused by a database CHECK constraint. Grant `audit:read`,
   `operations:read` and approver roles only to the people who need them.
7. **Scheduler service account.** Create an auth user for the scheduler and make it a member with only the
   `run_agent:<id>` permissions its workflows need. Set `CC_SCHEDULER_USER_ID`. Without it, scheduled agent
   workflows fail closed and their jobs are marked `FAILED`.
8. **Run the processes.** Run `serve`, at least one `worker` and two `scheduler` instances, all with the same
   environment. Point the platform's liveness probe at `/health/live` and its readiness probe at
   `/health/ready`.
9. **Set CORS.** Set `CC_CORS_ORIGINS` to the exact Lovable origins. Any other origin is refused (403 on
   preflight).

## Realtime

The browser consumes `GET /v1/events/stream` (Server-Sent Events) with an `Authorization: Bearer` header,
for example through `@microsoft/fetch-event-source`. Tokens in the URL are refused. Events come from the
organization's audit log, are scoped to the caller's server-side membership, and carry only an
allow-listed `payload_summary`. Reconnecting with `Last-Event-ID` (or `?after=<id>`) resumes without gaps.
`GET /v1/events?after=` is the polling equivalent. Supabase Realtime on raw tables is **not** used. Do not
enable it on governed tables: application authorization would be bypassed and unfiltered rows would stream.
