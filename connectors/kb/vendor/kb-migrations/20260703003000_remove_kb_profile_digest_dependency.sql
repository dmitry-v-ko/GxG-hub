CREATE OR REPLACE FUNCTION public.kb_profile_upsert(
  p_id uuid,
  p_workspace_id uuid,
  p_name text,
  p_description text,
  p_body_md text,
  p_activate boolean,
  p_updated_by text,
  p_checksum text
)
RETURNS TABLE (
  id uuid,
  workspace_id uuid,
  name text,
  description text,
  status text,
  version integer,
  body_md text,
  source text,
  checksum text,
  created_at timestamptz,
  updated_at timestamptz,
  updated_by text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public, pg_temp
AS $$
DECLARE
  v_profile kb.profiles%ROWTYPE;
  v_workspace_id uuid;
BEGIN
  IF nullif(trim(coalesce(p_body_md, '')), '') IS NULL THEN
    RAISE EXCEPTION 'body_md is required';
  END IF;

  IF p_id IS NULL THEN
    v_workspace_id := coalesce(p_workspace_id, '00000000-0000-0000-0000-000000000000'::uuid);

    IF p_activate THEN
      UPDATE kb.profiles
      SET status = 'draft', updated_by = p_updated_by
      WHERE kb.profiles.workspace_id = v_workspace_id
        AND kb.profiles.status = 'active';
    END IF;

    INSERT INTO kb.profiles (
      workspace_id,
      name,
      description,
      status,
      version,
      body_md,
      source,
      checksum,
      updated_by
    )
    VALUES (
      v_workspace_id,
      nullif(trim(coalesce(p_name, '')), ''),
      coalesce(p_description, ''),
      CASE WHEN p_activate THEN 'active' ELSE 'draft' END,
      1,
      p_body_md,
      'mcp',
      coalesce(p_checksum, md5(p_body_md)),
      coalesce(nullif(trim(p_updated_by), ''), 'kb-access')
    )
    RETURNING * INTO v_profile;
  ELSE
    SELECT * INTO v_profile
    FROM kb.profiles
    WHERE profiles.id = p_id
    FOR UPDATE;

    IF NOT FOUND THEN
      RAISE EXCEPTION 'KB profile % not found', p_id;
    END IF;

    v_workspace_id := coalesce(p_workspace_id, v_profile.workspace_id);

    IF p_activate THEN
      UPDATE kb.profiles
      SET status = 'draft', updated_by = p_updated_by
      WHERE kb.profiles.workspace_id = v_workspace_id
        AND kb.profiles.status = 'active'
        AND kb.profiles.id <> p_id;
    END IF;

    UPDATE kb.profiles
    SET
      workspace_id = v_workspace_id,
      name = nullif(trim(coalesce(p_name, kb.profiles.name)), ''),
      description = coalesce(p_description, kb.profiles.description),
      status = CASE WHEN p_activate THEN 'active' ELSE kb.profiles.status END,
      version = kb.profiles.version + 1,
      body_md = p_body_md,
      checksum = coalesce(p_checksum, md5(p_body_md)),
      updated_by = coalesce(nullif(trim(p_updated_by), ''), 'kb-access')
    WHERE kb.profiles.id = p_id
    RETURNING * INTO v_profile;
  END IF;

  INSERT INTO kb.profile_versions (
    profile_id,
    workspace_id,
    version,
    body_md,
    checksum,
    created_by
  )
  VALUES (
    v_profile.id,
    v_profile.workspace_id,
    v_profile.version,
    v_profile.body_md,
    v_profile.checksum,
    v_profile.updated_by
  );

  RETURN QUERY
  SELECT
    p.id,
    p.workspace_id,
    p.name,
    p.description,
    p.status,
    p.version,
    p.body_md,
    p.source,
    p.checksum,
    p.created_at,
    p.updated_at,
    p.updated_by
  FROM kb.profiles p
  WHERE p.id = v_profile.id;
END;
$$;

CREATE OR REPLACE FUNCTION public.kb_profile_activate(
  p_id uuid,
  p_updated_by text DEFAULT 'kb-access'
)
RETURNS TABLE (
  id uuid,
  workspace_id uuid,
  name text,
  description text,
  status text,
  version integer,
  body_md text,
  source text,
  checksum text,
  created_at timestamptz,
  updated_at timestamptz,
  updated_by text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public, pg_temp
AS $$
DECLARE
  v_profile kb.profiles%ROWTYPE;
BEGIN
  SELECT * INTO v_profile
  FROM kb.profiles
  WHERE profiles.id = p_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'KB profile % not found', p_id;
  END IF;

  UPDATE kb.profiles
  SET status = 'draft', updated_by = coalesce(nullif(trim(p_updated_by), ''), 'kb-access')
  WHERE kb.profiles.workspace_id = v_profile.workspace_id
    AND kb.profiles.status = 'active'
    AND kb.profiles.id <> p_id;

  UPDATE kb.profiles
  SET status = 'active', updated_by = coalesce(nullif(trim(p_updated_by), ''), 'kb-access')
  WHERE kb.profiles.id = p_id
  RETURNING * INTO v_profile;

  RETURN QUERY
  SELECT
    p.id,
    p.workspace_id,
    p.name,
    p.description,
    p.status,
    p.version,
    p.body_md,
    p.source,
    p.checksum,
    p.created_at,
    p.updated_at,
    p.updated_by
  FROM kb.profiles p
  WHERE p.id = v_profile.id;
END;
$$;

CREATE OR REPLACE FUNCTION public.kb_profile_history(p_id uuid)
RETURNS TABLE (
  id uuid,
  profile_id uuid,
  workspace_id uuid,
  version integer,
  body_md text,
  checksum text,
  created_at timestamptz,
  created_by text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = kb, public, pg_temp
AS $$
  SELECT
    v.id,
    v.profile_id,
    v.workspace_id,
    v.version,
    v.body_md,
    v.checksum,
    v.created_at,
    v.created_by
  FROM kb.profile_versions v
  WHERE v.profile_id = p_id
  ORDER BY v.version DESC;
$$;

REVOKE ALL ON FUNCTION public.kb_profile_get_active(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.kb_profile_list(uuid, boolean) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.kb_profile_upsert(uuid, uuid, text, text, text, boolean, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.kb_profile_activate(uuid, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.kb_profile_history(uuid) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.kb_profile_get_active(uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.kb_profile_list(uuid, boolean) TO service_role;
GRANT EXECUTE ON FUNCTION public.kb_profile_upsert(uuid, uuid, text, text, text, boolean, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.kb_profile_activate(uuid, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.kb_profile_history(uuid) TO service_role;
