-- Merge Service (6), Source (8), Product (9) → Tool (5): they are all the same concept
UPDATE kb_entities SET entity_type_id = 5 WHERE entity_type_id IN (6, 8, 9);
UPDATE kb_entity_types SET is_active = false WHERE id IN (6, 8, 9);

-- Create taxonomy axis for entity types (sort_order=0 → appears first in Browse/FilterPanel)
INSERT INTO kb_taxonomy_axes (workspace_id, key, label, description, is_system, is_hierarchical, sort_order, is_active)
VALUES (
  '00000000-0000-0000-0000-000000000000',
  'entity_type',
  'Type',
  'Тип записи в базе знаний',
  true,
  false,
  0,
  true
)
ON CONFLICT DO NOTHING;

-- Create taxonomy terms mirroring each active entity type (excluding internal feedback_log=13)
INSERT INTO kb_taxonomy_terms (axis_id, parent_id, key, label, description, weight, is_active)
SELECT
  (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type'),
  NULL,
  et.key,
  et.label,
  et.description,
  et.id,
  true
FROM kb_entity_types et
WHERE et.is_active = true
  AND et.id NOT IN (13)
ON CONFLICT DO NOTHING;

-- tool_category is now a sub-axis of Tool: move it after Pillar/Domain in FilterPanel
UPDATE kb_taxonomy_axes SET sort_order = 4 WHERE key = 'tool_category';

-- Reparent all tool_category terms under the 'tool' entity_type term
-- This enables Browse path: Type → Tool → Voice & Sound → TTS
UPDATE kb_taxonomy_terms
SET parent_id = (
  SELECT id FROM kb_taxonomy_terms
  WHERE axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
    AND key = 'tool'
  LIMIT 1
)
WHERE axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'tool_category');

-- Tag all existing entities with their entity_type taxonomy term
INSERT INTO kb_entity_taxonomy (entity_id, term_id)
SELECT
  e.id,
  t.id
FROM kb_entities e
JOIN kb_entity_types et ON et.id = e.entity_type_id
JOIN kb_taxonomy_terms t
  ON t.key = et.key
  AND t.axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
ON CONFLICT DO NOTHING;
