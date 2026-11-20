-- Phase 4: durable jobs, conversations, connector status, query indexes.

-- ---------------------------------------------------------------- jobs (durable queue)
CREATE TABLE jobs (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  job_type        text NOT NULL,
  idempotency_key text NOT NULL,
  side_effect     text NOT NULL CHECK (side_effect IN ('READ_ONLY','INTERNAL','EXTERNAL_WRITE')),
  run_id          uuid,
  workflow_run_id uuid,
  priority        integer NOT NULL DEFAULT 0,
  status          text NOT NULL CHECK (status IN ('QUEUED','CLAIMED','SUCCEEDED','FAILED','CANCELLED','ABANDONED')),
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts    integer NOT NULL CHECK (max_attempts >= 1),
  timeout_seconds integer NOT NULL CHECK (timeout_seconds > 0),
  available_at    timestamptz NOT NULL,
  claimed_by      text,
  lease_expires_at timestamptz,
  last_error      text,
  result_ref      text,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, idempotency_key),          -- duplicate submissions never create a second job
  UNIQUE (organization_id, id),
  CHECK (side_effect <> 'EXTERNAL_WRITE' OR max_attempts = 1),
  CHECK (attempts <= max_attempts),
  CHECK (status <> 'CLAIMED' OR (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)),
  FOREIGN KEY (organization_id, run_id) REFERENCES agent_runs(organization_id, id)
);
CREATE INDEX jobs_ready ON jobs (priority DESC, available_at) WHERE status = 'QUEUED';
CREATE INDEX jobs_leases ON jobs (lease_expires_at) WHERE status = 'CLAIMED';
CREATE INDEX jobs_org_status ON jobs (organization_id, status, created_at);
CREATE TRIGGER jobs_touch BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION cc_touch_updated_at();

-- ---------------------------------------------------------------- conversations
CREATE TABLE conversations (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  title           text NOT NULL,
  created_by      text NOT NULL,
  primary_agent_id text,
  status          text NOT NULL CHECK (status IN ('OPEN','ARCHIVED')),
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, id),
  FOREIGN KEY (organization_id, primary_agent_id) REFERENCES agents(organization_id, agent_key)
);
CREATE INDEX conversations_org_updated ON conversations (organization_id, updated_at DESC, id);
CREATE TRIGGER conversations_touch BEFORE UPDATE ON conversations FOR EACH ROW EXECUTE FUNCTION cc_touch_updated_at();
CREATE TABLE conversation_participants (
  organization_id uuid NOT NULL,
  conversation_id uuid NOT NULL,
  actor_type      text NOT NULL CHECK (actor_type IN ('HUMAN','AGENT','SYSTEM')),
  actor_id        text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, conversation_id, actor_type, actor_id),
  FOREIGN KEY (organization_id, conversation_id) REFERENCES conversations(organization_id, id) ON DELETE CASCADE
);
CREATE TABLE messages (
  id              uuid PRIMARY KEY,
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL,
  seq             bigint NOT NULL CHECK (seq > 0),
  sender_type     text NOT NULL CHECK (sender_type IN ('HUMAN','AGENT','SYSTEM')),
  sender_id       text NOT NULL,
  agent_id        text,
  content         text NOT NULL,
  structured_content jsonb,
  attachment_ids  text[] NOT NULL DEFAULT '{}',
  run_id          uuid,
  record          jsonb NOT NULL,
  version         integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL,
  UNIQUE (organization_id, id),
  UNIQUE (organization_id, conversation_id, seq),
  FOREIGN KEY (organization_id, conversation_id) REFERENCES conversations(organization_id, id),
  FOREIGN KEY (organization_id, run_id) REFERENCES agent_runs(organization_id, id)
);
CREATE INDEX messages_chronological ON messages (organization_id, conversation_id, seq);
CREATE TRIGGER messages_immutable BEFORE UPDATE OR DELETE ON messages FOR EACH ROW EXECUTE FUNCTION cc_append_only();

-- ---------------------------------------------------------------- connector status
CREATE TABLE connector_status (
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  view_name       text NOT NULL,
  last_success_at timestamptz,
  last_error_at   timestamptz,
  last_error      text,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, view_name)
);

-- ---------------------------------------------------------------- query indexes (database-side filtering)
CREATE INDEX agent_runs_org_status_created ON agent_runs (organization_id, status, created_at DESC, id);
CREATE INDEX agent_runs_org_created ON agent_runs (organization_id, created_at DESC, id);
CREATE INDEX tasks_org_status_created ON tasks (organization_id, status, created_at, id);
CREATE INDEX tasks_org_owner ON tasks (organization_id, owner, created_at, id);
CREATE INDEX approvals_org_status ON approvals (organization_id, status, requested_at, id);
CREATE INDEX outputs_org_agent_created ON outputs (organization_id, agent_key, created_at DESC, id);
CREATE INDEX outputs_org_kind_created ON outputs (organization_id, kind, created_at DESC, id);
CREATE INDEX recommendations_org_status ON recommendations (organization_id, status, created_at, id);
CREATE INDEX alerts_org_status_created ON alerts (organization_id, status, created_at, id);
CREATE INDEX audit_events_org_seq ON audit_events (organization_id, org_sequence);
CREATE INDEX kpi_snapshots_org_key_recorded ON kpi_snapshots (organization_id, kpi_key, created_at DESC);
