ALTER FUNCTION kb.update_updated_at() SET search_path = kb, pg_temp;
ALTER FUNCTION kb.generate_slug() SET search_path = kb, pg_temp;
ALTER FUNCTION kb.auto_version_entity() SET search_path = kb, pg_temp;
ALTER FUNCTION public.trigger_set_timestamps() SET search_path = public, pg_temp;

DROP POLICY IF EXISTS "feedback_insert_all" ON public.kb_entity_feedback;
DROP POLICY IF EXISTS "feedback_delete_all" ON public.kb_entity_feedback;
