-- Excerpt of gxg_agents_v2 supabase/migrations/20260930010000_capability_hub_replacement.sql @ f1c4d6d (lines below,
-- verbatim): the one table the telegram_archive migrations reference, plus the schemas they need.
-- Stand and tests only; the real database already has them.
CREATE SCHEMA capability_hub;
CREATE SCHEMA extensions;
CREATE TABLE capability_hub.connections (
  id text PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  tenancy_mode text NOT NULL CHECK (tenancy_mode IN ('shared', 'dedicated')),
  workspace_id text CHECK (workspace_id IS NULL OR workspace_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  credential_ref text NOT NULL CHECK (credential_ref ~ '^[A-Za-z][A-Za-z0-9+.-]*://[^[:space:]]{1,240}$'),
  credential_version text NOT NULL DEFAULT repeat('0', 64) CHECK (credential_version ~ '^[a-f0-9]{64}$'),
  enabled boolean NOT NULL DEFAULT true,
  revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT connections_tenancy_owner_check CHECK (
    (tenancy_mode = 'shared' AND workspace_id IS NULL)
    OR (tenancy_mode = 'dedicated' AND workspace_id IS NOT NULL)
  )
);
