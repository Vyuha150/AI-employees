-- Supabase Row Level Security for the AI Business Command Center.
-- Apply AFTER the core migrations, on Supabase (uses auth.uid() and the `authenticated` role).
-- See docs/supabase_rls.md for the model. Summary:
--   * Clients (browser, `authenticated` role) get READ access to their own organizations only.
--   * Every mutation goes through the backend API, which runs the governance layer
--     (approvals, risk, audit) and connects with a server-side credential.
--   * The service-role key bypasses RLS: it is a server secret and must never reach a browser.
--   * Audit, approval and role/permission tables are never writable by clients.

CREATE SCHEMA IF NOT EXISTS app;

-- Profiles (one per auth user). Clients may read and update only their own display name.
CREATE TABLE IF NOT EXISTS profiles (
  id           uuid PRIMARY KEY,            -- = auth.users.id
  display_name text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION app.is_member(org uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (SELECT 1 FROM organization_members m
                 WHERE m.organization_id = org AND m.user_id = auth.uid() AND m.status = 'ACTIVE')
$$;

CREATE OR REPLACE FUNCTION app.has_permission(org uuid, perm text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM organization_members m
    JOIN member_roles mr ON mr.organization_id = m.organization_id AND mr.member_id = m.id
    JOIN permissions p ON p.organization_id = mr.organization_id AND p.role_id = mr.role_id
    WHERE m.organization_id = org AND m.user_id = auth.uid() AND m.status = 'ACTIVE' AND p.permission = perm)
$$;

REVOKE ALL ON FUNCTION app.is_member(uuid), app.has_permission(uuid, text) FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO authenticated;
GRANT EXECUTE ON FUNCTION app.is_member(uuid), app.has_permission(uuid, text) TO authenticated;

-- Deny-by-default: enable and FORCE RLS on every table, revoke everything from client roles.
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON %I FROM anon, authenticated', t);
  END LOOP;
END $$;

-- profiles: own row only; display name is the only client-writable column.
GRANT SELECT, UPDATE (display_name) ON profiles TO authenticated;
CREATE POLICY profiles_self_read ON profiles FOR SELECT TO authenticated USING (id = auth.uid());
CREATE POLICY profiles_self_update ON profiles FOR UPDATE TO authenticated USING (id = auth.uid()) WITH CHECK (id = auth.uid());

-- organizations & membership: members see their organizations and co-members. No client writes:
-- membership, roles and permission changes go through a privileged backend path (audited).
GRANT SELECT ON organizations, organization_members, roles, permissions, member_roles TO authenticated;
CREATE POLICY orgs_member_read ON organizations FOR SELECT TO authenticated USING (app.is_member(id));
CREATE POLICY members_read ON organization_members FOR SELECT TO authenticated USING (app.is_member(organization_id));
CREATE POLICY roles_read ON roles FOR SELECT TO authenticated USING (app.is_member(organization_id));
CREATE POLICY permissions_read ON permissions FOR SELECT TO authenticated USING (app.is_member(organization_id));
CREATE POLICY member_roles_read ON member_roles FOR SELECT TO authenticated USING (app.is_member(organization_id));

-- Business objects: read-only for members of the owning organization.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['agents','agent_versions','agent_tools','agent_schedules','agent_runs','tool_calls','tasks',
    'task_dependencies','task_events','approvals','approval_events','outputs','output_versions','evaluations',
    'recommendations','recommendation_events','change_requests','kpis','kpi_snapshots','kpi_outcomes','memories',
    'memory_events','delegations','workflows','workflow_steps','workflow_runs','scheduled_runs','alerts',
    'notifications','ai_usage','knowledge_documents','conversations','conversation_participants','messages']
  LOOP
    EXECUTE format('GRANT SELECT ON %I TO authenticated', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR SELECT TO authenticated USING (app.is_member(organization_id))',
                   t || '_member_read', t);
  END LOOP;
END $$;

-- Knowledge chunks: members, and only for documents the member's roles may read.
GRANT SELECT ON knowledge_chunks TO authenticated;
CREATE POLICY knowledge_chunks_read ON knowledge_chunks FOR SELECT TO authenticated USING (
  app.is_member(organization_id) AND EXISTS (
    SELECT 1 FROM knowledge_documents d WHERE d.organization_id = knowledge_chunks.organization_id
      AND d.id = knowledge_chunks.document_id
      AND ('ALL' = ANY(d.permissions) OR EXISTS (
        SELECT 1 FROM organization_members m JOIN member_roles mr ON mr.organization_id = m.organization_id AND mr.member_id = m.id
        JOIN roles r ON r.organization_id = mr.organization_id AND r.id = mr.role_id
        WHERE m.organization_id = d.organization_id AND m.user_id = auth.uid() AND r.name = ANY(d.permissions)))));

-- Audit: readable only with the audit:read permission. Never writable by any client
-- (no grant), and the append-only trigger rejects UPDATE/DELETE even for privileged roles.
GRANT SELECT ON audit_events TO authenticated;
CREATE POLICY audit_read ON audit_events FOR SELECT TO authenticated
  USING (app.has_permission(organization_id, 'audit:read'));

-- Not exposed to clients at all: sources, idempotency_keys, attachments (served via signed
-- URLs by the backend), schema_migrations, jobs, connector_status, rate_limit_counters,
-- process_heartbeats. (RLS is enabled on them by the loop above and no policy grants access.)
-- Apply this file AFTER all core migrations (0001, 0002, 0003) so every table is covered.
