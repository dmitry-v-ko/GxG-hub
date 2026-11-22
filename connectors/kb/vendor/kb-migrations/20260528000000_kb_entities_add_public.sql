-- Add public flag to kb.entities
-- Only entities with public = true will be visible in the public catalog

ALTER TABLE kb.entities
  ADD COLUMN IF NOT EXISTS public boolean NOT NULL DEFAULT false;

-- All existing articles are public
UPDATE kb.entities SET public = true;

-- Expose public column through the view
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
