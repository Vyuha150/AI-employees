# Supabase / PostgreSQL Row Level Security

The policies are in `src/command_center/resources/migrations/supabase/0101_rls.sql`. Apply it after the core
migration (`apply_migrations()` applies `0001_core.sql`).

## Principles

1. **The frontend never bypasses backend authorization.** Browser clients (Supabase `authenticated` role)
   get **read-only** access to their own organizations' rows. Every write goes through the backend API
   (`command_center.api.ApiService`), so all writes run the governance layer: gateway, risk policy,
   approvals and audit.
2. **Deny by default.** RLS is enabled **and forced** on every table. `anon` and `authenticated` lose all
   privileges, then only the `SELECT` grants listed below come back.
3. **The service-role key is a server secret.** It bypasses RLS. It lives only in the backend's
   environment (never in config files, never in a browser bundle). The backend still scopes every query by
   `organization_id` itself (`PgRepositories` is bound to one organization), so RLS is a second,
   independent layer, not the only one.
4. **Audit and approvals are immutable from clients.**
   - Clients have no write grants on `audit_events`, `approvals` or the event tables.
   - The append-only triggers in `0001_core.sql` reject `UPDATE`, `DELETE` and `TRUNCATE` on audit tables
     even for privileged roles.
   - A trigger keeps an approval's binding fields (payload hash, tool, run, agent, risk class, expiry)
     unchangeable. Check constraints forbid self-approval and approvals that never expire.
5. **Role and permission changes need a privileged backend path.** Clients can read `roles`, `permissions`
   and `member_roles`, but can never write them.
   - The backend endpoint for these changes must require the organization's administrator role.
   - It must write a `configuration.changed` audit event.
   - It must never be callable with an agent identity: no agent profile can be granted `modify_permissions`.

## Helper functions

| Function | Meaning |
|---|---|
| `app.is_member(org uuid)` | The caller (`auth.uid()`) is an ACTIVE member of `org` |
| `app.has_permission(org uuid, perm text)` | The caller holds `perm` through one of its roles in `org` |

Both are `SECURITY DEFINER` with a fixed `search_path`, so policies can consult membership without giving
clients direct access to the underlying joins.

## Policies

| Table(s) | Client access | Policy |
|---|---|---|
| `profiles` | read and update own row (`display_name` only) | `id = auth.uid()` |
| `organizations`, `organization_members`, `roles`, `permissions`, `member_roles` | read | `app.is_member(organization_id)` |
| `tasks`, `task_dependencies`, `task_events` | read | member of the owning organization |
| `outputs`, `output_versions`, `evaluations` | read | member |
| `approvals`, `approval_events` | read; **no writes** | member |
| `recommendations`, `recommendation_events`, `change_requests` | read | member |
| `kpis`, `kpi_snapshots`, `kpi_outcomes` | read | member |
| `agent_runs`, `tool_calls`, `delegations`, `workflow*`, `scheduled_runs` | read | member |
| `agents`, `agent_versions`, `agent_tools`, `agent_schedules` | read | member |
| `alerts`, `notifications`, `ai_usage`, `memories`, `memory_events`, `knowledge_documents` | read | member |
| `knowledge_chunks` | read | member **and** the document is open to `ALL` or one of the caller's roles |
| `audit_events` | read | `app.has_permission(organization_id, 'audit:read')` |
| `sources`, `idempotency_keys`, `attachments`, `schema_migrations` | none | backend only (attachments are served through signed URLs) |

There are no `INSERT`, `UPDATE` or `DELETE` policies for clients on business tables. Approving an action,
rejecting a recommendation or creating a task are backend API calls, which apply the policy-defined approver
role and write the audit trail.

## Realtime

Browsers subscribe to the backend's SSE stream (`GET /v1/events/stream`, see `docs/deployment.md`), not to
raw table changes. Realtime payloads carry an allow-listed `payload_summary` only: no prompts, tool
parameters or model text. Supabase Realtime is not used. If you enable it anyway, enable it only on tables
whose RLS read policy is acceptable to stream, and never on `audit_events`, `jobs` or `rate_limit_counters`.

## Verification

`tests/test_postgres.py::test_supabase_rls_isolates_tenants` applies these policies to a real PostgreSQL
server, with a stub `auth.uid()` that reads `request.jwt.claim.sub`, as Supabase does. It verifies:
- a member of organization A sees only A's tasks;
- the member reads no audit rows without `audit:read`;
- the member cannot update approvals;
- the member cannot grant permissions.

Run it with `CC_TEST_DATABASE_URL` pointing at an admin DSN. **These policies have not yet been applied to a
real Supabase project.** `docs/deployment.md` lists the steps for doing that and checking the result.

## Deployment checklist

- [ ] Core migrations `0001`–`0003` applied (`ai-command-center migrate`); `0101_rls.sql` applied after them.
- [ ] The backend connects with a server-side role. The service-role key is only in server environment
      variables, never in `NEXT_PUBLIC_*` or similar client-exposed variables.
- [ ] `organization_members.user_id` rows exist for every user. Membership changes run through the
      privileged, audited backend path.
- [ ] `audit:read` granted only to the roles that need it.
- [ ] Realtime is enabled only on tables whose read policy is safe to stream.
