-- Add Content Seed as a first-class active KB entity type.
--
-- This keeps storage in kb_entities, but stops treating seeds as Blueprint records.
-- ContentSeed remains an internal draft planning entity, not a public article.

UPDATE kb_entity_types
SET
  label = 'Content Seed',
  description = 'Internal content planning entity: a short idea, angle, claim, or content prompt with source/supporting links. Not a public article and not published as-is.',
  category = 'planning',
  body_mode = 'markdown',
  is_versioned = true,
  is_indexed = true,
  is_active = true,
  settings = '{
    "version": "2026-07-03",
    "purpose": "Help agents store reusable content ideas extracted from research, calls, articles, videos, and other source material.",
    "decision_test": "Is this a short reusable content angle or idea with source/supporting links, rather than a complete article or how-to?",
    "required_fields": ["target_pillar", "icp_interest", "supporting_links", "public_ready"],
    "use_when": ["content idea", "pillar angle", "ICP question", "belief shift", "contrarian take", "case angle", "tool angle", "workflow angle"],
    "do_not_use_when": ["complete knowledge article", "reusable how-to workflow", "named tool profile", "published-ready article"],
    "visibility": "internal_draft_only"
  }'::jsonb
WHERE key = 'content_seed';

INSERT INTO kb_entity_types (
  key,
  label,
  description,
  category,
  body_mode,
  is_versioned,
  is_indexed,
  is_active,
  settings
)
SELECT
  'content_seed',
  'Content Seed',
  'Internal content planning entity: a short idea, angle, claim, or content prompt with source/supporting links. Not a public article and not published as-is.',
  'planning',
  'markdown',
  true,
  true,
  true,
  '{
    "version": "2026-07-03",
    "purpose": "Help agents store reusable content ideas extracted from research, calls, articles, videos, and other source material.",
    "decision_test": "Is this a short reusable content angle or idea with source/supporting links, rather than a complete article or how-to?",
    "required_fields": ["target_pillar", "icp_interest", "supporting_links", "public_ready"],
    "use_when": ["content idea", "pillar angle", "ICP question", "belief shift", "contrarian take", "case angle", "tool angle", "workflow angle"],
    "do_not_use_when": ["complete knowledge article", "reusable how-to workflow", "named tool profile", "published-ready article"],
    "visibility": "internal_draft_only"
  }'::jsonb
WHERE NOT EXISTS (
  SELECT 1 FROM kb_entity_types WHERE key = 'content_seed'
);

WITH content_seed_type AS (
  SELECT id FROM kb_entity_types WHERE key = 'content_seed' LIMIT 1
),
entity_axis AS (
  SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type'
)
INSERT INTO kb_taxonomy_terms (
  axis_id,
  key,
  label,
  description,
  weight,
  is_active
)
SELECT
  entity_axis.id,
  'content_seed',
  'Content Seed',
  'Internal content planning entity with source/supporting links. Not a public article.',
  content_seed_type.id,
  true
FROM entity_axis, content_seed_type
WHERE NOT EXISTS (
  SELECT 1
  FROM kb_taxonomy_terms
  WHERE axis_id = entity_axis.id
    AND key = 'content_seed'
);

UPDATE kb_taxonomy_terms
SET
  label = 'Content Seed',
  description = 'Internal content planning entity with source/supporting links. Not a public article.',
  weight = (SELECT id FROM kb_entity_types WHERE key = 'content_seed' LIMIT 1),
  is_active = true
WHERE axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
  AND key = 'content_seed';

-- Move existing v1 seed records from Blueprint+marker to the first-class type.
UPDATE kb_entities
SET entity_type_id = (SELECT id FROM kb_entity_types WHERE key = 'content_seed')
WHERE data->>'object_type' = 'content_seed';

-- Refresh entity_type taxonomy assignments only for content seeds.
DELETE FROM kb_entity_taxonomy
WHERE entity_id IN (
  SELECT id FROM kb_entities WHERE data->>'object_type' = 'content_seed'
)
AND term_id IN (
  SELECT id
  FROM kb_taxonomy_terms
  WHERE axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
);

INSERT INTO kb_entity_taxonomy (entity_id, term_id)
SELECT e.id, t.id
FROM kb_entities e
JOIN kb_taxonomy_terms t
  ON t.axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
  AND t.key = 'content_seed'
WHERE e.data->>'object_type' = 'content_seed'
ON CONFLICT DO NOTHING;
