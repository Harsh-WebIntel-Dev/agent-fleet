-- 003_rls_hardening.sql — make Row Level Security actually enforce.
--
-- WHY THIS EXISTS. Migrations 001/002 enabled RLS on the client-scoped tables and the plan
-- described it as "the strongest isolation property in the design". It was never enforcing.
-- Proven 2026-08-18 by inserting two rows under different client_ids, scoping the session to
-- tenant A with set_config('app.client_id', ...), and selecting: BOTH rows came back.
--
-- Three independent reasons, each sufficient on its own:
--   1. fleet_admin has rolbypassrls = true  -> PostgreSQL skips RLS entirely for it
--   2. fleet_admin has rolsuper     = true  -> superusers bypass RLS
--   3. relforcerowsecurity = false          -> the TABLE OWNER bypasses its own RLS policies,
--                                              and fleet_admin owns every one of these tables
--
-- The application must therefore NOT connect as fleet_admin. fleet_admin stays as the migration
-- / DDL role; fleet_app is the runtime role and is deliberately unprivileged.

-- 1. Runtime role: no superuser, no BYPASSRLS, no table ownership.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_app') THEN
    CREATE ROLE fleet_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END $$;

ALTER ROLE fleet_app NOSUPERUSER NOBYPASSRLS;

-- 2. Least-privilege grants. DML only — no DDL, no ownership.
GRANT USAGE ON SCHEMA app, memory, client_kb, skill_kb TO fleet_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app, memory, client_kb, skill_kb TO fleet_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app, memory, client_kb, skill_kb TO fleet_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA app, memory, client_kb, skill_kb
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fleet_app;

-- 3. FORCE RLS so even the owner is subject to policy. Without this, owner queries skip policies
--    silently — which is exactly how this went unnoticed.
ALTER TABLE app.runs              FORCE ROW LEVEL SECURITY;
ALTER TABLE app.client_agent_keys FORCE ROW LEVEL SECURITY;
ALTER TABLE client_kb.entries     FORCE ROW LEVEL SECURITY;
ALTER TABLE memory.entries        FORCE ROW LEVEL SECURITY;

-- 4. skill_kb is deliberately NOT client-scoped: it is the shared, client-agnostic tier that every
--    specialist reads. Left without RLS on purpose. Do not "fix" this.
