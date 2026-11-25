-- gxg_kb_reader: the only database role of gxg-kb-service (M6, ADR-0006, AC-M6-04), for the GxG Hub pilot (M13).
-- The OWNER reviews this file and applies it to the Supabase KB database as `postgres` (SQL editor or psql). Nothing in
-- the deploy runs it. Idempotent: safe to run again. NO password here: set it afterwards out of band
-- (`\password gxg_kb_reader`) and store it as KB_DATABASE_PASSWORD of the Railway service `kb`.
-- Same grants as gxg-kb-service schema/roles/gxg_kb_reader.sql; the service reads exactly these columns (src/kb_service/backend.py)
-- and its /readyz fails if the role has any write path. No DROP, DELETE or TRUNCATE of data in this file.

SET client_min_messages = warning;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gxg_kb_reader') THEN
    CREATE ROLE gxg_kb_reader LOGIN;
  END IF;
END
$$;

ALTER ROLE gxg_kb_reader WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 20;
ALTER ROLE gxg_kb_reader SET default_transaction_read_only = on;
ALTER ROLE gxg_kb_reader SET statement_timeout = '5s';
ALTER ROLE gxg_kb_reader SET lock_timeout = '1s';
ALTER ROLE gxg_kb_reader SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE gxg_kb_reader SET search_path = '';  -- every name the service uses is schema-qualified

-- No memberships: anon / authenticated / service_role would bring their ALL grants (or BYPASSRLS).
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
           WHERE m.member = 'gxg_kb_reader'::regrole LOOP
    EXECUTE format('REVOKE %I FROM gxg_kb_reader', r.rolname);
  END LOOP;
END
$$;

-- Start from nothing, then grant exactly what the service reads.
REVOKE ALL ON ALL TABLES IN SCHEMA kb FROM gxg_kb_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA kb FROM gxg_kb_reader;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA kb FROM gxg_kb_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM gxg_kb_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM gxg_kb_reader;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM gxg_kb_reader;
REVOKE ALL ON SCHEMA kb FROM gxg_kb_reader;
REVOKE ALL ON SCHEMA public FROM gxg_kb_reader;

GRANT USAGE ON SCHEMA kb TO gxg_kb_reader;
GRANT SELECT (id, workspace_id, entity_type_id, title, summary, body_md, status, archived_at, public, updated_at,
              search_vector)
  ON kb.entities TO gxg_kb_reader;
GRANT SELECT (id, workspace_id, key, is_active) ON kb.entity_types TO gxg_kb_reader;

-- RLS is on for both tables and the existing policies are TO anon/authenticated/service_role, so the role needs its own:
-- published, not archived, public rows of the workspace the service sets per transaction (`SET LOCAL gxg.workspace_id`);
-- not set: no row. Created once, then ALTERed on every run (no DROP POLICY).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'kb' AND tablename = 'entities'
                 AND policyname = 'gxg_kb_reader_select') THEN
    CREATE POLICY gxg_kb_reader_select ON kb.entities AS PERMISSIVE FOR SELECT TO gxg_kb_reader USING (false);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'kb' AND tablename = 'entity_types'
                 AND policyname = 'gxg_kb_reader_select') THEN
    CREATE POLICY gxg_kb_reader_select ON kb.entity_types AS PERMISSIVE FOR SELECT TO gxg_kb_reader USING (false);
  END IF;
END
$$;
ALTER POLICY gxg_kb_reader_select ON kb.entities TO gxg_kb_reader
  USING (status = 'published' AND archived_at IS NULL AND public
         AND workspace_id = nullif(pg_catalog.current_setting('gxg.workspace_id', true), '')::uuid);
ALTER POLICY gxg_kb_reader_select ON kb.entity_types TO gxg_kb_reader
  USING (is_active IS TRUE
         AND workspace_id IN (nullif(pg_catalog.current_setting('gxg.workspace_id', true), '')::uuid,
                              '00000000-0000-0000-0000-000000000000'::uuid)
         AND nullif(pg_catalog.current_setting('gxg.workspace_id', true), '') IS NOT NULL);
