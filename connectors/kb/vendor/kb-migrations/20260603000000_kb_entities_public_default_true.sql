-- Change public column default to true so new articles are visible immediately
-- Previously defaulted to false, causing new articles to be hidden from the public KB

ALTER TABLE kb.entities
  ALTER COLUMN public SET DEFAULT true;

-- Recreate the view to ensure it reflects current schema
CREATE OR REPLACE VIEW public.kb_entities AS
 SELECT id,
    workspace_id,
    entity_type_id,
    title,
    slug,
    summary,
    body_md,
    status,
    confidence,
    visibility,
    owner_user_id,
    data,
    provenance,
    current_version_no,
    created_at,
    updated_at,
    archived_at,
    public
   FROM kb.entities;
