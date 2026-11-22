-- Normalize KB entity types to the five-type operating model and add agent-facing guidance.

UPDATE kb_entity_types
SET
  key = 'insight',
  label = 'Insights',
  description = 'Market/work knowledge: observations, trends, strategic beliefs, and explanations of why something matters. Use when there is no primary step-by-step method or named tool profile.',
  category = 'knowledge',
  body_mode = 'markdown',
  is_versioned = true,
  is_indexed = true,
  is_active = true,
  settings = '{
    "version": "2026-06-28",
    "purpose": "Help agents choose entity_type_id when adding a new KB article.",
    "decision_test": "Does this mainly explain what is happening, why it matters, or what belief should change?",
    "use_when": ["market trend", "work pattern", "strategic observation", "belief shift", "business implication"],
    "do_not_use_when": ["article is a how-to workflow", "article is a named tool profile", "article is a growth tactic", "article is our measured experiment"],
    "examples": ["market shift from model quality to infrastructure", "vendor lock-in observation", "why teams fail with agent memory"]
  }'::jsonb
WHERE id = 1;

UPDATE kb_entity_types
SET
  key = 'growth_hack',
  label = 'Growth Hack',
  description = 'Growth tactics: non-obvious methods for acquisition, conversion, distribution, SEO/AEO/GEO, GTM, sales, outreach, content growth, or revenue operations.',
  category = 'knowledge',
  body_mode = 'markdown',
  is_versioned = true,
  is_indexed = true,
  is_active = true,
  settings = '{
    "version": "2026-06-28",
    "purpose": "Help agents choose entity_type_id when adding a new KB article.",
    "decision_test": "Can a growth, sales, marketing, SEO, or GTM operator apply this tactic to get more pipeline, reach, traffic, conversion, or revenue?",
    "use_when": ["SEO/GEO/AEO tactic", "outreach tactic", "GTM playbook", "content distribution method", "conversion or pipeline hack"],
    "do_not_use_when": ["generic operating workflow with no growth outcome", "named tool profile", "market observation without a tactic"],
    "examples": ["omni-channel outreach sequencing", "Google ranking tactic", "AI-search SEO optimization workflow"]
  }'::jsonb
WHERE id = 2;

UPDATE kb_entity_types
SET
  key = 'experiment',
  label = 'Experiment',
  description = 'Our experiment results: internal tests with hypothesis, setup, measured result, learning, and decision. Keep empty until we add our own experiments.',
  category = 'knowledge',
  body_mode = 'markdown',
  is_versioned = true,
  is_indexed = true,
  is_active = true,
  settings = '{
    "version": "2026-06-28",
    "purpose": "Help agents choose entity_type_id when adding a new KB article.",
    "decision_test": "Is this a result of our own experiment with measurable setup and outcome?",
    "required_fields": ["hypothesis", "setup", "metric", "result", "decision_or_next_step"],
    "do_not_use_when": ["external experiment", "benchmark from another author", "tool evaluation without our measured result"],
    "examples": ["our Clay outbound test result", "our LinkedIn carousel experiment result"]
  }'::jsonb
WHERE id = 3;

UPDATE kb_entity_types
SET
  key = 'tool',
  label = 'Tool',
  description = 'One concrete service, repository, library, API, platform, or tool per article. If one article covers many tools, split it or use Blueprint for the map/list.',
  category = 'operations',
  body_mode = 'markdown',
  is_versioned = true,
  is_indexed = true,
  is_active = true,
  settings = '{
    "version": "2026-06-28",
    "purpose": "Help agents choose entity_type_id when adding a new KB article.",
    "decision_test": "Is the main object one named tool/repo/service/API/platform that someone can use or install?",
    "use_when": ["software profile", "GitHub repo", "API/service", "platform", "library", "MCP server", "agent/tooling product"],
    "do_not_use_when": ["list of many tools", "workflow using several tools", "market insight about tools"],
    "examples": ["Firecrawl", "n8n", "Claude Code Router", "Headroom"]
  }'::jsonb
WHERE id = 5;

UPDATE kb_entity_types
SET
  key = 'blueprint',
  label = 'Blueprint',
  description = 'Reusable how-to knowledge: workflows, frameworks, checklists, templates, architectures, methods, MOCs, and operational playbooks.',
  category = 'knowledge',
  body_mode = 'markdown',
  is_versioned = true,
  is_indexed = true,
  is_active = true,
  settings = '{
    "version": "2026-06-28",
    "purpose": "Help agents choose entity_type_id when adding a new KB article.",
    "decision_test": "Does this teach how to do something useful or provide a reusable operating structure?",
    "use_when": ["how-to guide", "workflow", "framework", "checklist", "template", "architecture", "methodology", "MOC/map"],
    "do_not_use_when": ["named tool profile", "pure market observation", "growth-specific tactic", "our measured experiment"],
    "examples": ["agent scaffolding guide", "LLM-as-Judge architecture", "KB curation blueprint", "tool category MOC"]
  }'::jsonb
WHERE id = 12;

UPDATE kb_entity_types
SET is_active = false
WHERE id IN (4, 6, 7, 8, 9, 10, 11, 13);

-- Move existing entries out of inactive/legacy types.
UPDATE kb_entities SET entity_type_id = 5 WHERE entity_type_id IN (5, 6, 9);
UPDATE kb_entities SET entity_type_id = 12 WHERE entity_type_id IN (3, 4, 7, 10, 11, 13);

UPDATE kb_entities SET entity_type_id = 12 WHERE id IN (
  2, 4, 7, 8, 14, 17, 21, 22, 23, 27, 28, 29, 32, 34, 35, 39, 40, 41, 48, 53, 54,
  67, 82, 90, 103, 104, 105, 106, 107, 108, 109, 110, 111, 384, 506, 507, 508, 509,
  526, 528, 552, 554, 558, 561, 563, 567, 570, 573
);

UPDATE kb_entities SET entity_type_id = 2 WHERE id IN (
  38, 56, 57, 58, 59, 60, 61, 62, 72, 383, 512, 513, 556, 557
);

UPDATE kb_entities SET entity_type_id = 5 WHERE id IN (80, 379);
UPDATE kb_entities SET entity_type_id = 1 WHERE id IN (16, 46, 63, 77, 79, 543);

-- Keep the Type taxonomy in sync with the active entity types.
UPDATE kb_taxonomy_terms
SET key = et.key,
    label = et.label,
    description = et.description,
    is_active = et.is_active,
    parent_id = NULL
FROM kb_entity_types et
WHERE kb_taxonomy_terms.axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
  AND kb_taxonomy_terms.weight = et.id;

UPDATE kb_taxonomy_terms
SET is_active = false
WHERE axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
  AND weight NOT IN (1, 2, 3, 5, 12);

DELETE FROM kb_entity_taxonomy
WHERE term_id IN (
  SELECT id FROM kb_taxonomy_terms
  WHERE axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
);

INSERT INTO kb_entity_taxonomy (entity_id, term_id)
SELECT e.id, t.id
FROM kb_entities e
JOIN kb_taxonomy_terms t
  ON t.axis_id = (SELECT id FROM kb_taxonomy_axes WHERE key = 'entity_type')
  AND t.weight = e.entity_type_id
  AND t.is_active = true
ON CONFLICT DO NOTHING;

UPDATE kb_entities
SET
  title = 'KB Curation Blueprint v3',
  summary = 'Operating rules for classifying KB articles, choosing one of five active Types, assigning taxonomy tags, and creating useful relations.',
  entity_type_id = 12,
  body_md = $blueprint$
# KB Curation Blueprint v3

## Entity Type Decision Rules

When adding a new article, choose exactly one primary `Type`. These five active types are the only allowed values.

### Decision Order

1. **Experiment** — use only for our own experiment results with hypothesis, setup, metric, result, and decision. Do not use for external benchmarks or videos.
2. **Tool** — use when the article is about one named service, repository, library, API, platform, MCP server, or concrete instrument. Rule: one article = one tool. If an article covers many tools, split it into multiple Tool entries or make the overview a Blueprint.
3. **Growth Hack** — use when the article teaches a non-obvious growth tactic for acquisition, conversion, distribution, SEO/AEO/GEO, GTM, sales, outreach, content growth, or revenue operations.
4. **Blueprint** — use when the article is reusable how-to knowledge: workflow, framework, checklist, template, architecture, methodology, operating method, or MOC/map.
5. **Insights** — use when the article mainly captures market/work knowledge: trend, observation, strategic belief, belief shift, or explanation of why something matters.

### Type Table

| Type | Use When | Do Not Use When |
|---|---|---|
| `Insights` | Market/work observation, trend, strategic belief, business implication | It teaches steps, profiles a tool, or gives a growth tactic |
| `Growth Hack` | A practical tactic to grow reach, traffic, pipeline, conversion, sales, or revenue | It is a generic workflow with no growth outcome, or a named tool profile |
| `Blueprint` | How-to, workflow, framework, checklist, architecture, method, template, MOC | It is one named tool, pure market observation, or our measured experiment |
| `Experiment` | Our internal measured experiment with result and decision | External case study, benchmark, demo, or opinion |
| `Tool` | One named service/repo/API/platform/library/tool | A list of tools or a workflow that uses several tools |

### Anti-Rules

- Never use inactive legacy types: `Prompt`, `Note`, `Source`, `Policy`, `FAQ Entry`, `Content Plan Item`, `Feedback Log`, `Service`, `Product`.
- A source article, transcript, YouTube summary, or web page is not automatically `Source`; classify by what the KB entry is useful for.
- A prompt collection is usually `Blueprint` if it is a reusable playbook, or `Growth Hack` if it is specifically for GTM/SEO/sales/content growth.
- A tool roundup is `Blueprint`; each individual tool from it can become a separate `Tool` entry.
- If unsure between `Insights` and `Blueprint`: choose `Blueprint` only when a competent operator could follow it as a method.
- If unsure between `Blueprint` and `Growth Hack`: choose `Growth Hack` only when the primary outcome is growth.

## Taxonomy Tags

After choosing `Type`, add taxonomy tags separately. Type answers “what kind of KB object is this?” Taxonomy answers “what topic/domain/category does it belong to?”

Use active taxonomy axes such as `Pillar`, `Domain`, and `Tool Category` where applicable. Do not replace `Type` with tags, and do not create a new type just because a new topic appears.

## Relations

Use relations only when they add useful navigation.

| Source | Target | Relation | When |
|---|---|---|---|
| Article | Source material | `derived_from` | The KB entry is extracted from a transcript, article, video, repo, or external page |
| Blueprint/Growth Hack/Insights | Tool | `references` | The article uses or discusses a concrete tool |
| Tool | Tool | `alternative_to` | Two tools solve the same job |
| Newer entry | Older entry | `supersedes` | The new entry replaces the old one |
| Any entry | Any entry | `related_to` | Strong topical relation without a stricter relation type |

## Agent Stop Condition

Before saving a new article, the agent should be able to state:

- chosen `Type`
- one-sentence reason for that type
- why the closest alternative type was rejected
- required taxonomy tags or why they are missing

If the distinction is still ambiguous after this check, ask the user with 2-3 concrete options.
$blueprint$
WHERE id = 1;
