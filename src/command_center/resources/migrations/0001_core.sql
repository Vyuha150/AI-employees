-- AI Business Command Center: core schema (PostgreSQL 14+ / Supabase compatible)
-- Conventions
--   * UUID primary keys (gen_random_uuid()).
--   * Every organization-owned table has organization_id and UNIQUE (organization_id, id);
--     foreign keys between organization-owned tables are COMPOSITE (organization_id, x_id) so a
--     row can never reference another organization's row.
--   * Agent ids are organization-scoped: agents are keyed (organization_id, agent_key).
--   * `record` jsonb holds the full domain record; typed columns carry the fields used for
--     integrity (FKs, CHECKs) and lookup. `version` is the optimistic-concurrency counter.
--   * Audit and event tables are append-only (triggers reject UPDATE and DELETE).

CREATE SCHEMA IF NOT EXISTS connector;   -- organization-provided read views (customers, leads, ...)

CREATE OR REPLACE FUNCTION cc_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

CREATE OR REPLACE FUNCTION cc_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'insufficient_privilege'; END $$;

-- ---------------------------------------------------------------- organizations & access
CREATE TABLE organizations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9_\-]{2,64}$'),
  name        text NOT NULL,
  timezone    text NOT NULL,
  currency    char(3) NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE organization_members (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id         uuid NOT NULL,                  -- auth.users.id in Supabase
  display_name    text,
  status          text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, user_id), UNIQUE (organization_id, id)
);
CREATE TABLE roles (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name            text NOT NULL CHECK (name ~ '^[A-Z][A-Z0-9_]{1,63}$'),
  description     text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, name), UNIQUE (organization_id, id)
);
CREATE TABLE permissions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL,
  role_id         uuid NOT NULL,
  permission      text NOT NULL CHECK (permission !~ '\*'),   -- no wildcards, ever
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, role_id, permission),
  FOREIGN KEY (organization_id, role_id) REFERENCES roles(organization_id, id) ON DELETE CASCADE
);
CREATE TABLE member_roles (
  organization_id uuid NOT NULL,
  member_id       uuid NOT NULL,
  role_id         uuid NOT NULL,
  granted_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, member_id, role_id),
  FOREIGN KEY (organization_id, member_id) REFERENCES organization_members(organization_id, id) ON DELETE CASCADE,
  FOREIGN KEY (organization_id, role_id) REFERENCES roles(organization_id, id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------- agents
CREATE TABLE agents (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  agent_key       text NOT NULL CHECK (agent_key ~ '^[a-z][a-z0-9_]{1,63}$'),
  role            text NOT NULL,
  name            text NOT NULL,
  enabled         boolean NOT NULL,
  reports_to      text,
  current_version_id uuid,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, agent_key), UNIQUE (organization_id, id), UNIQUE (organization_id, role),
  FOREIGN KEY (organization_id, reports_to) REFERENCES agents(organization_id, agent_key) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE agent_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL,
  agent_key       text NOT NULL,
  profile_hash    char(64) NOT NULL,
  profile         jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, agent_key, profile_hash), UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, agent_key) REFERENCES agents(organization_id, agent_key) ON DELETE CASCADE
);
CREATE TABLE agent_tools (
  organization_id uuid NOT NULL,
  agent_key       text NOT NULL,
  tool_name       text NOT NULL,
  relation        text NOT NULL CHECK (relation IN ('ALLOWED','PROHIBITED','ALWAYS_REQUIRE_APPROVAL')),
  PRIMARY KEY (organization_id, agent_key, tool_name, relation),
  FOREIGN KEY (organization_id, agent_key) REFERENCES agents(organization_id, agent_key) ON DELETE CASCADE
);
CREATE TABLE agent_schedules (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL,
  agent_key       text NOT NULL,
  job_id          text NOT NULL,
  workflow        text NOT NULL,
  cron            text NOT NULL,
  timezone        text NOT NULL,
  enabled         boolean NOT NULL,
  missed_run_policy  text NOT NULL CHECK (missed_run_policy IN ('SKIP','RUN_ONCE','CATCH_UP')),
  concurrency_policy text NOT NULL CHECK (concurrency_policy IN ('FORBID','ALLOW')),
  watermark       timestamptz,
  next_run_at     timestamptz,
  last_run_at     timestamptz,
  record          jsonb,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, job_id), UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, agent_key) REFERENCES agents(organization_id, agent_key) ON DELETE CASCADE
);

-- ---------------------------------------------------------------- runs & tool calls
CREATE TABLE agent_runs (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  agent_key       text NOT NULL,
  status          text NOT NULL CHECK (status IN ('QUEUED','RUNNING','TOOL_WAIT','APPROVAL_REQUIRED','SUCCEEDED','FAILED','CANCELLED')),
  triggered_by    text NOT NULL,
  parent_run_id   uuid,
  depth           integer NOT NULL DEFAULT 0 CHECK (depth >= 0),
  retry_of        uuid,
  tokens_used     bigint NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
  cost_used       numeric(18,6) NOT NULL DEFAULT 0 CHECK (cost_used >= 0),
  model_provider  text,
  model_name      text,
  fallback_used   boolean NOT NULL DEFAULT false,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, agent_key) REFERENCES agents(organization_id, agent_key),
  FOREIGN KEY (organization_id, parent_run_id) REFERENCES agent_runs(organization_id, id),
  FOREIGN KEY (organization_id, retry_of) REFERENCES agent_runs(organization_id, id)
);
CREATE INDEX agent_runs_org_agent_created ON agent_runs (organization_id, agent_key, created_at DESC);

CREATE TABLE tool_calls (                         -- filled from audit_events by trigger
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  audit_event_id  uuid NOT NULL UNIQUE,
  run_id          uuid,
  agent_key       text,
  tool_name       text NOT NULL,
  status          text NOT NULL,
  risk_class      text,
  approval_id     uuid,
  execution_id    text,
  tool_call_id    text,
  result_ref      text,
  error           text,
  mock            boolean NOT NULL DEFAULT false,
  executed_at     timestamptz NOT NULL,
  UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, run_id) REFERENCES agent_runs(organization_id, id)
);

-- ---------------------------------------------------------------- tasks
CREATE TABLE tasks (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  title           text NOT NULL CHECK (length(title) > 0),
  owner           text NOT NULL,
  owner_type      text NOT NULL CHECK (owner_type IN ('HUMAN','AGENT','SYSTEM')),
  priority        text NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
  status          text NOT NULL CHECK (status IN ('BACKLOG','QUEUED','IN_PROGRESS','WAITING_HUMAN','WAITING_AGENT','BLOCKED','COMPLETED','CANCELLED')),
  risk_level      text NOT NULL CHECK (risk_level IN ('GREEN','AMBER','RED')),
  due_date        date,
  source_run_id   uuid,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  CHECK (status <> 'BLOCKED' OR (record->>'blocked_reason') IS NOT NULL),
  FOREIGN KEY (organization_id, source_run_id) REFERENCES agent_runs(organization_id, id)
);
CREATE TABLE task_dependencies (
  organization_id uuid NOT NULL,
  task_id         uuid NOT NULL,
  depends_on      uuid NOT NULL,
  PRIMARY KEY (organization_id, task_id, depends_on),
  CHECK (task_id <> depends_on),
  FOREIGN KEY (organization_id, task_id) REFERENCES tasks(organization_id, id) ON DELETE CASCADE,
  FOREIGN KEY (organization_id, depends_on) REFERENCES tasks(organization_id, id)
);

-- ---------------------------------------------------------------- approvals
CREATE TABLE approvals (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  run_id          uuid NOT NULL,
  agent_key       text NOT NULL,
  tool_name       text NOT NULL,
  action_type     text NOT NULL,
  payload_hash    char(64) NOT NULL,
  risk_class      text NOT NULL CHECK (risk_class IN ('GREEN','AMBER','RED')),
  required_role   text NOT NULL,
  status          text NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED','EXECUTED','FAILED','EXECUTION_UNKNOWN')),
  execution_status text NOT NULL CHECK (execution_status IN ('NOT_STARTED','IN_PROGRESS','SUCCEEDED','FAILED','UNKNOWN')),
  requested_by    text NOT NULL,
  approved_by     text,
  requested_at    timestamptz NOT NULL,
  expires_at      timestamptz NOT NULL,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  CHECK (expires_at > requested_at),
  CHECK (risk_class <> 'RED' OR expires_at <= requested_at + interval '7 days'),   -- never permanent
  CHECK (approved_by IS NULL OR approved_by <> requested_by),                       -- no self-approval
  CHECK (status NOT IN ('APPROVED','EXECUTED','FAILED','EXECUTION_UNKNOWN') OR approved_by IS NOT NULL),
  FOREIGN KEY (organization_id, run_id) REFERENCES agent_runs(organization_id, id),
  FOREIGN KEY (organization_id, agent_key) REFERENCES agents(organization_id, agent_key)
);
-- payload_hash, tool, run and agent of an approval can never change after creation
CREATE OR REPLACE FUNCTION cc_approval_binding_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.payload_hash <> OLD.payload_hash OR NEW.tool_name <> OLD.tool_name OR NEW.run_id <> OLD.run_id
     OR NEW.agent_key <> OLD.agent_key OR NEW.action_type <> OLD.action_type OR NEW.risk_class <> OLD.risk_class
     OR NEW.requested_at <> OLD.requested_at OR NEW.expires_at <> OLD.expires_at OR NEW.required_role <> OLD.required_role THEN
    RAISE EXCEPTION 'approval binding fields are immutable' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.status IN ('REJECTED','EXPIRED','CANCELLED','EXECUTED','FAILED') AND NEW.status <> OLD.status THEN
    RAISE EXCEPTION 'approval in terminal state % cannot change', OLD.status USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approvals_binding BEFORE UPDATE ON approvals FOR EACH ROW EXECUTE FUNCTION cc_approval_binding_immutable();

-- ---------------------------------------------------------------- outputs & evaluations
CREATE TABLE outputs (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  run_id          uuid NOT NULL,
  agent_key       text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('STRUCTURED_RESPONSE','DAILY_BRIEF','WEEKLY_REVIEW')),
  validity        text NOT NULL CHECK (validity IN ('VALID','INVALID')),
  content_hash    char(64) NOT NULL,
  ingested        boolean NOT NULL DEFAULT false,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  CHECK (NOT ingested OR (validity = 'VALID' AND kind = 'STRUCTURED_RESPONSE')),
  FOREIGN KEY (organization_id, run_id) REFERENCES agent_runs(organization_id, id)
);
CREATE OR REPLACE FUNCTION cc_output_content_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.content_hash <> OLD.content_hash OR NEW.validity <> OLD.validity OR NEW.kind <> OLD.kind
     OR (NEW.record->>'content_json') IS DISTINCT FROM (OLD.record->>'content_json') THEN
    RAISE EXCEPTION 'output content and validity are immutable' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER outputs_immutable BEFORE UPDATE ON outputs FOR EACH ROW EXECUTE FUNCTION cc_output_content_immutable();
CREATE TABLE output_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL,
  output_id       uuid NOT NULL,
  version_no      integer NOT NULL,
  content_hash    char(64) NOT NULL,
  content         jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, output_id, version_no),
  FOREIGN KEY (organization_id, output_id) REFERENCES outputs(organization_id, id) ON DELETE CASCADE
);
CREATE OR REPLACE FUNCTION cc_output_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO output_versions (organization_id, output_id, version_no, content_hash, content)
  VALUES (NEW.organization_id, NEW.id, 1, NEW.content_hash, (NEW.record->>'content_json')::jsonb);
  RETURN NEW;
END $$;
CREATE TRIGGER outputs_version AFTER INSERT ON outputs FOR EACH ROW EXECUTE FUNCTION cc_output_version();
CREATE TABLE evaluations (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL,
  output_id       uuid NOT NULL,
  reviewer        text NOT NULL,
  accuracy        smallint NOT NULL CHECK (accuracy BETWEEN 1 AND 5),
  relevance       smallint NOT NULL CHECK (relevance BETWEEN 1 AND 5),
  completeness    smallint NOT NULL CHECK (completeness BETWEEN 1 AND 5),
  actionability   smallint NOT NULL CHECK (actionability BETWEEN 1 AND 5),
  business_value  smallint NOT NULL CHECK (business_value BETWEEN 1 AND 5),
  composite_score numeric(4,2) NOT NULL,
  decision        text NOT NULL CHECK (decision IN ('APPROVED','NEEDS_REVISION','REJECTED')),
  comment         text,
  reviewed_at     timestamptz NOT NULL,
  CHECK (decision = 'APPROVED' OR coalesce(length(comment), 0) > 0),
  FOREIGN KEY (organization_id, output_id) REFERENCES outputs(organization_id, id)
);

-- ---------------------------------------------------------------- recommendations & changes
CREATE TABLE recommendations (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  source_agent    text NOT NULL,
  run_id          uuid,
  status          text NOT NULL CHECK (status IN ('PROPOSED','UNDER_REVIEW','APPROVED','REJECTED','IMPLEMENTATION','IMPLEMENTED','IMPACT_REVIEW','VERIFIED','ROLLED_BACK')),
  impact_category text NOT NULL,
  confidence      text NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW')),
  fingerprint     char(64) NOT NULL,
  decided_by      text,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  CHECK (status NOT IN ('APPROVED','REJECTED','VERIFIED') OR decided_by IS NOT NULL),
  FOREIGN KEY (organization_id, run_id) REFERENCES agent_runs(organization_id, id)
);
CREATE TABLE change_requests (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  recommendation_id uuid NOT NULL,
  status          text NOT NULL CHECK (status IN ('DRAFT','APPROVED','PLANNING','IMPLEMENTATION','TESTING','REVIEW','COMPLETED','IMPACT_MEASUREMENT','CLOSED','ROLLED_BACK')),
  owner           text NOT NULL,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  CHECK (length(record->>'rollback_plan') > 0 AND length(record->>'validation_method') > 0),
  FOREIGN KEY (organization_id, recommendation_id) REFERENCES recommendations(organization_id, id)
);

-- ---------------------------------------------------------------- KPIs
CREATE TABLE kpis (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  kpi_key         text NOT NULL,
  owner_agent     text,
  definition      text NOT NULL,
  direction       text NOT NULL CHECK (direction IN ('HIGHER_IS_BETTER','LOWER_IS_BETTER','TARGET_RANGE')),
  unit            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, kpi_key, owner_agent), UNIQUE (organization_id, id)
);
CREATE TABLE kpi_snapshots (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  kpi_key         text NOT NULL,
  value           numeric NOT NULL,
  unit            text NOT NULL,
  observed_at     text NOT NULL,
  source_reference text NOT NULL CHECK (length(source_reference) > 0),   -- no unsourced values
  recorded_by     text NOT NULL,
  run_id          uuid,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, run_id) REFERENCES agent_runs(organization_id, id)
);
CREATE INDEX kpi_snapshots_lookup ON kpi_snapshots (organization_id, kpi_key, observed_at DESC);
CREATE TABLE kpi_outcomes (                       -- filled from outcome.recorded audit events
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL,
  audit_event_id  uuid NOT NULL UNIQUE,
  change_id       uuid NOT NULL,
  kpi_key         text NOT NULL,
  baseline        numeric, target numeric, expected_value numeric, actual numeric, variance numeric,
  outcome_status  text NOT NULL CHECK (outcome_status IN ('NOT_MEASURED','PARTIAL','SUCCESS','FAILED','INCONCLUSIVE')),
  measured_by     text NOT NULL,
  recorded_at     timestamptz NOT NULL,
  FOREIGN KEY (organization_id, change_id) REFERENCES change_requests(organization_id, id)
);

-- ---------------------------------------------------------------- memory, sources, knowledge
CREATE TABLE memories (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  scope           text NOT NULL CHECK (scope IN ('ORGANIZATION','DEPARTMENT','AGENT','CUSTOMER','PROJECT')),
  scope_ref       text,
  status          text NOT NULL CHECK (status IN ('PROPOSED','ACCEPTED','REJECTED','INVALIDATED','EXPIRED')),
  proposed_by_agent text NOT NULL,
  decided_by      text,
  review_at       timestamptz,
  expires_at      timestamptz,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  CHECK (jsonb_array_length(record->'source_ids') > 0),                  -- memory must be sourced
  CHECK (status IN ('PROPOSED','EXPIRED') OR decided_by IS NOT NULL),
  CHECK (scope = 'ORGANIZATION' OR scope_ref IS NOT NULL)
);
CREATE TABLE sources (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  source_key      text NOT NULL,
  source_type     text NOT NULL CHECK (source_type IN ('COMPANY_DATABASE','DOCUMENT','USER_MESSAGE','TOOL_RESULT','WEB_SOURCE','SYSTEM_CONFIG','HUMAN_DECISION','ACCEPTED_MEMORY','AGENT_OUTPUT')),
  title           text,
  reference       text NOT NULL,
  retrieved_at    text,
  authority_level text CHECK (authority_level IN ('AUTHORITATIVE','SECONDARY','UNVERIFIED')),
  run_id          uuid,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, source_key, reference), UNIQUE (organization_id, id)
);
CREATE TABLE knowledge_documents (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  title           text NOT NULL,
  category        text NOT NULL,
  permissions     text[] NOT NULL CHECK (cardinality(permissions) > 0),
  source_reference text NOT NULL,
  valid_from      timestamptz, valid_until timestamptz,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  UNIQUE (organization_id, id),
  CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from)
);
CREATE TABLE knowledge_chunks (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL,
  document_id     uuid NOT NULL,
  ordinal         integer NOT NULL,
  text            text NOT NULL,
  embedding       jsonb,                         -- reserved for a future embedding retriever
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  UNIQUE (organization_id, document_id, ordinal), UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, document_id) REFERENCES knowledge_documents(organization_id, id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------- delegation & workflows
CREATE TABLE delegations (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  parent_run_id   uuid NOT NULL,
  child_run_id    uuid,
  requested_by_agent text NOT NULL,
  target_agent    text NOT NULL,
  status          text NOT NULL CHECK (status IN ('REQUESTED','RUNNING','COMPLETED','FAILED','CANCELLED')),
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  CHECK (requested_by_agent <> target_agent),
  FOREIGN KEY (organization_id, parent_run_id) REFERENCES agent_runs(organization_id, id),
  FOREIGN KEY (organization_id, child_run_id) REFERENCES agent_runs(organization_id, id),
  FOREIGN KEY (organization_id, target_agent) REFERENCES agents(organization_id, agent_key)
);
CREATE TABLE workflows (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name            text NOT NULL,
  definition_version integer NOT NULL CHECK (definition_version > 0),
  status          text NOT NULL CHECK (status IN ('DRAFT','ACTIVE','PAUSED','ARCHIVED')),
  created_by      text NOT NULL,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id), UNIQUE (organization_id, name, definition_version)
);
CREATE TABLE workflow_steps (
  organization_id uuid NOT NULL,
  workflow_id     uuid NOT NULL,
  step_id         text NOT NULL,
  step_type       text NOT NULL CHECK (step_type IN ('AGENT','TOOL','CONDITION','APPROVAL','HUMAN_TASK','NOTIFICATION','DELAY','KPI_UPDATE','END')),
  config          jsonb NOT NULL,
  next_step       text,
  PRIMARY KEY (organization_id, workflow_id, step_id),
  CHECK (NOT (config ?| array['skip_approval','bypass_approval','approval_required','auto_approve','risk_class','force'])),
  FOREIGN KEY (organization_id, workflow_id) REFERENCES workflows(organization_id, id) ON DELETE CASCADE
);
CREATE TABLE workflow_runs (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  workflow_id     uuid NOT NULL,
  status          text NOT NULL CHECK (status IN ('QUEUED','RUNNING','WAITING','SUCCEEDED','FAILED','CANCELLED')),
  current_step    text,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, workflow_id) REFERENCES workflows(organization_id, id)
);
CREATE TABLE scheduled_runs (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  job_id          text NOT NULL,
  scheduled_for   timestamptz NOT NULL,
  status          text NOT NULL CHECK (status IN ('TRIGGERED','SKIPPED','DUPLICATE','SUCCEEDED','FAILED')),
  idempotency_key text NOT NULL,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, job_id) REFERENCES agent_schedules(organization_id, job_id)
);
CREATE UNIQUE INDEX scheduled_runs_one_trigger ON scheduled_runs (organization_id, idempotency_key) WHERE status = 'TRIGGERED';

-- ---------------------------------------------------------------- alerts & notifications
CREATE TABLE alerts (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  severity        text NOT NULL CHECK (severity IN ('INFO','WATCH','WARNING','CRITICAL')),
  category        text NOT NULL,
  title           text NOT NULL CHECK (length(title) > 0),
  status          text NOT NULL CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED','DISMISSED')),
  dedupe_key      text,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id)
);
CREATE UNIQUE INDEX alerts_open_dedupe ON alerts (organization_id, dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('OPEN','ACKNOWLEDGED');
CREATE TABLE notifications (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  recipient       text NOT NULL,
  channel         text NOT NULL,
  alert_id        uuid,
  status          text NOT NULL CHECK (status IN ('PENDING','SENT','FAILED')),
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, alert_id) REFERENCES alerts(organization_id, id)
);

-- ---------------------------------------------------------------- usage, idempotency, attachments
CREATE TABLE ai_usage (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  run_id          uuid NOT NULL,
  agent_key       text NOT NULL,
  provider        text NOT NULL,
  model           text NOT NULL,
  input_tokens    integer NOT NULL CHECK (input_tokens >= 0),
  output_tokens   integer NOT NULL CHECK (output_tokens >= 0),
  cache_read_tokens  integer CHECK (cache_read_tokens >= 0),
  cache_write_tokens integer CHECK (cache_write_tokens >= 0),
  tool_calls      integer NOT NULL DEFAULT 0,
  estimated_cost  numeric(18,6),                 -- NULL when the cost cannot be determined
  cost_explanation text NOT NULL,
  currency        char(3),
  fallback_used   boolean NOT NULL DEFAULT false,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  UNIQUE (organization_id, id),
  CHECK (estimated_cost IS NULL OR currency IS NOT NULL),
  FOREIGN KEY (organization_id, run_id) REFERENCES agent_runs(organization_id, id)
);
CREATE INDEX ai_usage_budget ON ai_usage (organization_id, agent_key, created_at);
CREATE TABLE idempotency_keys (
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  key             text NOT NULL,
  result_ref      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, key)
);
CREATE TABLE attachments (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name            text NOT NULL,
  mime_type       text NOT NULL,
  size_bytes      bigint NOT NULL CHECK (size_bytes > 0),
  storage_reference text NOT NULL,
  uploaded_by     text NOT NULL,
  classification  text NOT NULL CHECK (classification IN ('PUBLIC','INTERNAL','CONFIDENTIAL','RESTRICTED')),
  scan_status     text NOT NULL CHECK (scan_status IN ('PENDING','CLEAN','INFECTED','FAILED')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id)
);

-- ---------------------------------------------------------------- audit (append-only, hash-chained)
CREATE TABLE audit_events (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id),
  org_sequence    bigint NOT NULL CHECK (org_sequence > 0),
  occurred_at     timestamptz NOT NULL,
  actor_type      text NOT NULL CHECK (actor_type IN ('HUMAN','AGENT','SYSTEM')),
  actor_id        text NOT NULL,
  agent_key       text,
  run_id          uuid,
  event_type      text NOT NULL,
  object_type     text NOT NULL,
  object_id       text NOT NULL,
  payload         jsonb,
  previous_state  text,
  new_state       text,
  prev_hash       char(64) NOT NULL,
  event_hash      char(64) NOT NULL UNIQUE,
  body            jsonb NOT NULL,                -- exact hashed body, for verification
  UNIQUE (organization_id, org_sequence)
);
CREATE INDEX audit_events_object ON audit_events (organization_id, object_type, object_id);
CREATE INDEX audit_events_type ON audit_events (organization_id, event_type, occurred_at);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION cc_append_only();
CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION cc_append_only();

CREATE TABLE approval_events       (LIKE audit_events INCLUDING DEFAULTS);
CREATE TABLE task_events           (LIKE audit_events INCLUDING DEFAULTS);
CREATE TABLE recommendation_events (LIKE audit_events INCLUDING DEFAULTS);
CREATE TABLE memory_events         (LIKE audit_events INCLUDING DEFAULTS);
ALTER TABLE approval_events ADD PRIMARY KEY (id);
ALTER TABLE task_events ADD PRIMARY KEY (id);
ALTER TABLE recommendation_events ADD PRIMARY KEY (id);
ALTER TABLE memory_events ADD PRIMARY KEY (id);
CREATE TRIGGER approval_events_ro BEFORE UPDATE OR DELETE ON approval_events FOR EACH ROW EXECUTE FUNCTION cc_append_only();
CREATE TRIGGER task_events_ro BEFORE UPDATE OR DELETE ON task_events FOR EACH ROW EXECUTE FUNCTION cc_append_only();
CREATE TRIGGER recommendation_events_ro BEFORE UPDATE OR DELETE ON recommendation_events FOR EACH ROW EXECUTE FUNCTION cc_append_only();
CREATE TRIGGER memory_events_ro BEFORE UPDATE OR DELETE ON memory_events FOR EACH ROW EXECUTE FUNCTION cc_append_only();

-- Route audit events into per-object event tables, tool_calls and kpi_outcomes.
CREATE OR REPLACE FUNCTION cc_route_audit_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.object_type = 'approval' THEN INSERT INTO approval_events SELECT NEW.*;
  ELSIF NEW.object_type = 'task' THEN INSERT INTO task_events SELECT NEW.*;
  ELSIF NEW.object_type = 'recommendation' THEN INSERT INTO recommendation_events SELECT NEW.*;
  ELSIF NEW.object_type = 'memory' THEN INSERT INTO memory_events SELECT NEW.*;
  END IF;
  IF NEW.event_type IN ('tool.executed','tool.failed','tool.execution_unknown') THEN
    INSERT INTO tool_calls (organization_id, audit_event_id, run_id, agent_key, tool_name, status, risk_class,
                            approval_id, execution_id, tool_call_id, result_ref, error, mock, executed_at)
    VALUES (NEW.organization_id, NEW.id, NEW.run_id, NEW.agent_key, NEW.object_id,
            coalesce(NEW.payload->>'status', CASE WHEN NEW.event_type = 'tool.executed' THEN 'OK' ELSE 'ERROR' END),
            NEW.payload->>'risk_class', nullif(NEW.payload->>'approval_id', '')::uuid, NEW.payload->>'execution_id',
            NEW.payload->>'tool_call_id', NEW.payload->>'result_ref', NEW.payload->>'error',
            coalesce((NEW.payload->>'mock')::boolean, false), NEW.occurred_at);
  END IF;
  IF NEW.event_type = 'outcome.recorded' THEN
    INSERT INTO kpi_outcomes (organization_id, audit_event_id, change_id, kpi_key, baseline, target, expected_value,
                              actual, variance, outcome_status, measured_by, recorded_at)
    VALUES (NEW.organization_id, NEW.id, NEW.object_id::uuid, NEW.payload->>'kpi', (NEW.payload->>'baseline')::numeric,
            (NEW.payload->>'target')::numeric, (NEW.payload->>'expected_value')::numeric,
            (NEW.payload->>'actual')::numeric, nullif(NEW.payload->>'variance', 'None')::numeric,
            NEW.payload->>'outcome_status', NEW.actor_id, NEW.occurred_at);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER audit_events_route AFTER INSERT ON audit_events FOR EACH ROW EXECUTE FUNCTION cc_route_audit_event();

-- updated_at maintenance
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['organizations','organization_members','agents','agent_schedules','agent_runs','tasks',
    'approvals','outputs','recommendations','change_requests','kpis','memories','delegations','workflows',
    'workflow_runs','alerts','notifications']
  LOOP
    EXECUTE format('CREATE TRIGGER %I_touch BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION cc_touch_updated_at()', t, t);
  END LOOP;
END $$;
