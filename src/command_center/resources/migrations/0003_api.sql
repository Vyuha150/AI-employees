-- Phase 5: API support tables.

-- Fixed-window rate limit counters shared by every API process.
CREATE TABLE rate_limit_counters (
  key           text NOT NULL,
  window_start  timestamptz NOT NULL,
  count         integer NOT NULL CHECK (count >= 0),
  PRIMARY KEY (key, window_start)
);
CREATE INDEX rate_limit_counters_window ON rate_limit_counters (window_start);

-- Worker / scheduler heartbeats for health reporting (no business data).
CREATE TABLE process_heartbeats (
  process_id    text PRIMARY KEY,
  kind          text NOT NULL CHECK (kind IN ('api','worker','scheduler')),
  is_leader     boolean NOT NULL DEFAULT false,
  started_at    timestamptz NOT NULL,
  last_seen_at  timestamptz NOT NULL
);

-- Membership lookup by user (API authentication path).
CREATE INDEX organization_members_user ON organization_members (user_id) WHERE status = 'ACTIVE';
