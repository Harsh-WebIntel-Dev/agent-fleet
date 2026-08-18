-- NemoClaw core schema init (fleet-core-postgres)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS app;        -- tasks, orchestration state
CREATE SCHEMA IF NOT EXISTS memory;     -- per-client working/conversation memory (vectors)
CREATE SCHEMA IF NOT EXISTS skill_kb;   -- shared, client-agnostic technique (promote-gated)
CREATE SCHEMA IF NOT EXISTS client_kb;  -- per-client siloed knowledge (vectors)

CREATE TABLE IF NOT EXISTS client_kb.entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id uuid NOT NULL,
  embedding vector(1024),
  content text,
  created_at timestamptz DEFAULT now()
);
ALTER TABLE client_kb.entries ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS client_isolation ON client_kb.entries;
CREATE POLICY client_isolation ON client_kb.entries
  USING (client_id = current_setting('app.client_id', true)::uuid);
-- Each request sets: SET app.client_id = '<acting client>'; before touching client_kb / memory.

CREATE TABLE IF NOT EXISTS memory.entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id uuid NOT NULL,
  session_key text NOT NULL,
  embedding vector(1024),
  content text,
  created_at timestamptz DEFAULT now()
);
ALTER TABLE memory.entries ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS client_isolation ON memory.entries;
CREATE POLICY client_isolation ON memory.entries
  USING (client_id = current_setting('app.client_id', true)::uuid);

-- skill_kb is shared/client-agnostic by design - no RLS, but writes only via the promotion gate
-- (see kb/promotion_gate), never directly from a specialist agent.
CREATE TABLE IF NOT EXISTS skill_kb.entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_client_id uuid, -- provenance only, not an access-control key
  embedding vector(1024),
  content text,
  promoted_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id uuid NOT NULL,
  clickup_task_id text,
  kind text NOT NULL, -- blog | gmb | social | newsletter | seo_fix | dev | ads | ...
  status text NOT NULL DEFAULT 'to_do',
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);
