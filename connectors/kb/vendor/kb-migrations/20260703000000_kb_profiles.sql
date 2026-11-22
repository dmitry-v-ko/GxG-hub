CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS kb.profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'archived')),
  version integer NOT NULL DEFAULT 1 CHECK (version > 0),
  body_md text NOT NULL,
  source text NOT NULL DEFAULT 'mcp',
  checksum text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text NOT NULL DEFAULT 'kb-access'
);

CREATE TABLE IF NOT EXISTS kb.profile_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id uuid NOT NULL REFERENCES kb.profiles(id) ON DELETE CASCADE,
  workspace_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  body_md text NOT NULL,
  checksum text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL DEFAULT 'kb-access',
  UNIQUE (profile_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS kb_profiles_one_active_per_workspace_idx
  ON kb.profiles (workspace_id)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS kb_profiles_workspace_status_idx
  ON kb.profiles (workspace_id, status);

CREATE INDEX IF NOT EXISTS kb_profile_versions_profile_version_idx
  ON kb.profile_versions (profile_id, version DESC);

DROP TRIGGER IF EXISTS kb_profiles_set_updated_at ON kb.profiles;
CREATE TRIGGER kb_profiles_set_updated_at
  BEFORE UPDATE ON kb.profiles
  FOR EACH ROW
  EXECUTE FUNCTION kb.update_updated_at();

ALTER TABLE kb.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.profile_versions ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE kb.profiles FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE kb.profile_versions FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON TABLE kb.profiles TO service_role;
GRANT SELECT, INSERT ON TABLE kb.profile_versions TO service_role;

CREATE OR REPLACE FUNCTION public.kb_profile_get_active(p_workspace_id uuid)
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
LANGUAGE sql
SECURITY DEFINER
SET search_path = kb, public, pg_temp
AS $$
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
  WHERE p.workspace_id = p_workspace_id
    AND p.status = 'active'
  LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION public.kb_profile_list(
  p_workspace_id uuid,
  p_include_archived boolean DEFAULT false
)
RETURNS TABLE (
  id uuid,
  workspace_id uuid,
  name text,
  description text,
  status text,
  version integer,
  source text,
  checksum text,
  created_at timestamptz,
  updated_at timestamptz,
  updated_by text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = kb, public, pg_temp
AS $$
  SELECT
    p.id,
    p.workspace_id,
    p.name,
    p.description,
    p.status,
    p.version,
    p.source,
    p.checksum,
    p.created_at,
    p.updated_at,
    p.updated_by
  FROM kb.profiles p
  WHERE p.workspace_id = p_workspace_id
    AND (p_include_archived OR p.status <> 'archived')
  ORDER BY (p.status <> 'active'), p.updated_at DESC;
$$;

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
