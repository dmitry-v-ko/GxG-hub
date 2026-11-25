CREATE OR REPLACE VIEW public.kb_entities
  WITH (security_barrier = true, security_invoker = true)
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

DO $$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'mastra_workspaces',
    'mastra_skills',
    'mastra_favorites',
    'mastra_skill_blobs',
    'mastra_background_tasks',
    'mastra_schedules',
    'mastra_notifications',
    'mastra_channel_installations',
    'mastra_tool_provider_connections',
    'mastra_schedule_triggers',
    'mastra_agents',
    'mastra_messages',
    'mastra_dataset_items',
    'mastra_mcp_server_versions',
    'mastra_prompt_block_versions',
    'mastra_scorer_definition_versions',
    'mastra_mcp_client_versions',
    'mastra_workspace_versions',
    'mastra_skill_versions',
    'mastra_channel_config',
    'mastra_agent_versions',
    'mastra_resources',
    'mastra_experiment_results',
    'mastra_dataset_versions',
    'mastra_scorers',
    'mastra_threads',
    'mastra_workflow_snapshot',
    'mastra_datasets',
    'mastra_experiments',
    'mastra_prompt_blocks',
    'mastra_ai_spans',
    'mastra_scorer_definitions',
    'mastra_mcp_clients',
    'mastra_mcp_servers'
  ]
  LOOP
    EXECUTE format('ALTER TABLE IF EXISTS public.%I ENABLE ROW LEVEL SECURITY', table_name);
  END LOOP;
END $$;
