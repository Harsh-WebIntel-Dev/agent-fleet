-- NemoClaw: client registry + run bookkeeping (Phase 3)
--
-- `app.clients.profile` is the ONLY source of the client context PM injects into specialists.
-- Keys inside it are what pipelines.yaml refers to as `client.*` — e.g. `client.blog_template`
-- resolves to profile->>'blog_template'. Specialists never read this table; PM reads it and
-- passes a narrowed copy (see memory.redact_for_specialist).

CREATE TABLE IF NOT EXISTS app.clients (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug            text UNIQUE NOT NULL,          -- also the LiteLLM team_alias
  display_name    text NOT NULL,
  litellm_team_id text,                          -- tenant boundary in the gateway
  profile         jsonb NOT NULL DEFAULT '{}'::jsonb,
  active          boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Per-agent virtual keys, one row per (client, agent). Stored so the orchestrator can pick the
-- right key per call without re-issuing. The key VALUE belongs in Infisical, not here — this
-- table holds only the reference.
CREATE TABLE IF NOT EXISTS app.client_agent_keys (
  client_id     uuid NOT NULL REFERENCES app.clients(id) ON DELETE CASCADE,
  agent_id      text NOT NULL,
  infisical_ref text NOT NULL,                   -- path in Infisical, NOT the key itself
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, agent_id)
);

-- Local mirror of the LiteLLM workflow-run ledger. LiteLLM is the source of truth for run
-- status/events; this exists so we can query "what is awaiting approval for client X" without
-- scanning the gateway, and so a paused run's resume state survives a gateway restart.
CREATE TABLE IF NOT EXISTS app.runs (
  run_id       text PRIMARY KEY,                 -- matches LiteLLM's run_id
  client_id    uuid NOT NULL REFERENCES app.clients(id) ON DELETE CASCADE,
  pipeline     text NOT NULL,
  status       text NOT NULL DEFAULT 'pending',
  paused_stage text,
  clickup_task_id text,
  resume_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runs_awaiting_idx
  ON app.runs (client_id, status) WHERE status = 'paused';

-- RLS on the client-scoped tables. app.clients itself is readable by the orchestrator (it must
-- resolve a slug before it knows the client_id), but the per-client rows are protected.
ALTER TABLE app.client_agent_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS client_isolation ON app.client_agent_keys;
CREATE POLICY client_isolation ON app.client_agent_keys
  USING (client_id = current_setting('app.client_id', true)::uuid);

ALTER TABLE app.runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS client_isolation ON app.runs;
CREATE POLICY client_isolation ON app.runs
  USING (client_id = current_setting('app.client_id', true)::uuid);

-- memory.entries / client_kb.entries already have RLS from 001. skill_kb deliberately does NOT —
-- it is the shared, client-agnostic tier and must be readable by every specialist.
