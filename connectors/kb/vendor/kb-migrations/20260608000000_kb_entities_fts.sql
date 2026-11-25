-- Full-text search for kb_entities
-- kb_entities is a view over kb.entities; column goes on the real table, view is recreated to expose it.
-- Uses 'simple' config: language-agnostic (Russian + English both work without separate dictionaries).

ALTER TABLE kb.entities
ADD COLUMN IF NOT EXISTS search_vector tsvector
GENERATED ALWAYS AS (
  setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
  setweight(to_tsvector('simple', coalesce(summary, '')), 'B') ||
  setweight(to_tsvector('simple', coalesce(body_md, '')), 'C')
) STORED;

CREATE INDEX IF NOT EXISTS kb_entities_search_vector_idx
ON kb.entities USING GIN(search_vector);

-- Expose search_vector through the public view so PostgREST / Supabase JS can filter on it
CREATE OR REPLACE VIEW public.kb_entities
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
    public,
    search_vector
  FROM kb.entities;
