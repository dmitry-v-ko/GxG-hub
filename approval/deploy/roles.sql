-- Cluster roles of the Approval Service (AS-27). Run once per cluster by the DBA (superuser), before
-- `gxg-approval migrate`. Roles are NOLOGIN here; the deployment turns on LOGIN and sets each password from
-- the secret store, one secret per process (AS-28). Idempotent.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['gxg_owner', 'gxg_api', 'gxg_mcp', 'gxg_ui', 'gxg_executor', 'gxg_sweeper',
                           'gxg_notifier', 'gxg_admin', 'gxg_decider'] LOOP
    BEGIN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', r);
    EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;  -- exists, or created concurrently
    END;
  END LOOP;
END $$;
-- Then, per database: CREATE DATABASE gxg_approval OWNER gxg_owner;
-- gxg_owner owns the schema and runs the migrations; no process connects as gxg_owner.
