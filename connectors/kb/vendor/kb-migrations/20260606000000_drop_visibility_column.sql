-- Drop unused visibility column from kb.entities
-- The column was never used in any application code; public/status fields cover all access control needs.

-- DROP + CREATE (not CREATE OR REPLACE) because pg won't allow removing columns via OR REPLACE
DROP VIEW IF EXISTS public.kb_entities CASCADE;
CREATE VIEW public.kb_entities
  WITH (security_barrier = true)
AS
  SELECT
    id,
    workspace_id,
    entity_type_id,
    title,
    slug,
    summary,
    body_md,
    status,
    confidence,
    owner_user_id,
    data,
    provenance,
    current_version_no,
    created_at,
    updated_at,
    archived_at,
    public
  FROM kb.entities;

ALTER TABLE kb.entities
  DROP COLUMN IF EXISTS visibility;
