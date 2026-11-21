-- Telegram message archive (T4c-1): an adapter-owned read model of the chats in each Telegram account
-- Connection's scope. Only the Telegram archive sync worker and archive Bindings read or write it; no
-- domain, agent, UI, anon or authenticated access. Purge on scope removal is enforced here, in the same
-- transaction as the Connection change (CON-TG-06).
--
-- The embedding ANN index is deferred to T4c-2, which writes the first embeddings: ivfflat trains its
-- list centroids from the rows present at build time, so an ivfflat index built on this empty table
-- would be degenerate (pgvector README, "IVFFlat": create the index after the table has some data).
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

CREATE SCHEMA telegram_archive;

-- Per-chat sync progress. A row exists only for an explicit scope chat that has been synced at least once.
CREATE TABLE telegram_archive.chats (
  connection_id text NOT NULL REFERENCES capability_hub.connections (id) ON DELETE CASCADE,
  peer_id bigint NOT NULL,
  chat_ref text NOT NULL CHECK (chat_ref ~ '^tgc1_[A-Za-z0-9_-]{32}$'),
  -- Folder-resolved members arrive with T4c-2 together with their membership purge rule.
  scope_source text NOT NULL DEFAULT 'chat' CHECK (scope_source = 'chat'),
  since date,
  state text NOT NULL DEFAULT 'backfilling' CHECK (state IN ('backfilling', 'synced', 'inaccessible')),
  synced_at timestamptz,
  -- Highest message id archived contiguously from the backfill frontier; 0 before the first pass.
  last_message_id bigint NOT NULL DEFAULT 0 CHECK (last_message_id >= 0),
  -- Lowest message id reached by backfill (exclusive frontier); NULL before the first pass.
  backfill_before_id bigint CHECK (backfill_before_id IS NULL OR backfill_before_id >= 0),
  -- True when the last pass could not cover its reconcile window; the next pass reconciles it.
  gap boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (connection_id, peer_id),
  UNIQUE (connection_id, chat_ref)
);

CREATE TABLE telegram_archive.messages (
  connection_id text NOT NULL,
  peer_id bigint NOT NULL,
  message_id bigint NOT NULL CHECK (message_id > 0),
  author_id bigint,
  sent_at timestamptz NOT NULL,
  edited_at timestamptz,
  text text CHECK (text IS NULL OR length(text) <= 8192),
  reply_to bigint CHECK (reply_to IS NULL OR reply_to > 0),
  media_type text CHECK (media_type IS NULL OR media_type ~ '^[a-z_]{1,32}$'),
  caption text CHECK (caption IS NULL OR length(caption) <= 8192),
  -- Russian and English full text, searchable on write (CON-TG-07).
  tsv tsvector GENERATED ALWAYS AS (
    to_tsvector('pg_catalog.russian'::regconfig, coalesce(text, '') || ' ' || coalesce(caption, ''))
    || to_tsvector('pg_catalog.english'::regconfig, coalesce(text, '') || ' ' || coalesce(caption, ''))
  ) STORED,
  embedding extensions.vector(1536),
  embedding_state text NOT NULL DEFAULT 'pending' CHECK (embedding_state IN ('pending', 'embedded', 'failed')),
  archived_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (connection_id, peer_id, message_id),
  FOREIGN KEY (connection_id, peer_id) REFERENCES telegram_archive.chats (connection_id, peer_id) ON DELETE CASCADE,
  CONSTRAINT messages_embedding_matches_state CHECK ((embedding IS NOT NULL) = (embedding_state = 'embedded'))
);

CREATE INDEX messages_tsv_idx ON telegram_archive.messages USING gin (tsv);

-- Account-wide sync deferral: a Telegram flood wait applies to the whole session, so the Connection is
-- skipped until `retry_after` (CON-TG-03: flood waits are honored, never retried early).
CREATE TABLE telegram_archive.connection_state (
  connection_id text PRIMARY KEY REFERENCES capability_hub.connections (id) ON DELETE CASCADE,
  retry_after timestamptz NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON SCHEMA telegram_archive IS 'Adapter-owned Telegram message archive; service role only.';
COMMENT ON TABLE telegram_archive.chats IS 'Per-chat archive sync progress for Telegram Connection scope chats.';
COMMENT ON TABLE telegram_archive.connection_state IS 'Per-Connection archive sync deferral after a Telegram flood wait.';
COMMENT ON TABLE telegram_archive.messages IS 'Archived Telegram messages of scope chats; deleted with their chat.';

-- One chat batch from `archive-sync`, applied atomically: the scope check, message upserts, reconcile
-- deletions, the progress advance and a flood-wait deferral commit together or not at all. The Connection
-- row is share-locked, so a concurrent scope change either waits for this batch (and then purges it) or
-- is seen and refuses it. `since` comes from that locked scope; a batch computed for another `since` is stale.
CREATE FUNCTION telegram_archive.apply_chat_batch(p_batch jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
DECLARE
  v_connection_id text := p_batch ->> 'connection_id';
  v_chat_ref text := p_batch ->> 'chat_ref';
  v_peer_id bigint;
  v_status text := p_batch ->> 'status';
  v_metadata jsonb;
  v_scope_chat jsonb;
  v_since date;
  v_since_at timestamptz;
  v_narrowed boolean := false;
  v_existing telegram_archive.chats;
  v_written integer := 0;
  v_deleted integer := 0;
  v_count integer;
BEGIN
  IF jsonb_typeof(p_batch) IS DISTINCT FROM 'object' OR v_connection_id IS NULL
    OR COALESCE(v_chat_ref, '') !~ '^tgc1_[A-Za-z0-9_-]{32}$' OR COALESCE(p_batch ->> 'peer_id', '') !~ '^-?[0-9]{1,19}$'
    OR v_status IS NULL OR v_status NOT IN ('ok', 'inaccessible', 'flood_wait')
    OR jsonb_typeof(p_batch -> 'messages') IS DISTINCT FROM 'array' OR jsonb_array_length(p_batch -> 'messages') > 1000
    OR jsonb_typeof(p_batch -> 'last_message_id') IS DISTINCT FROM 'number' OR (p_batch ->> 'last_message_id')::bigint < 0
    OR jsonb_typeof(p_batch -> 'backfill_complete') IS DISTINCT FROM 'boolean' OR jsonb_typeof(p_batch -> 'gap') IS DISTINCT FROM 'boolean'
    OR COALESCE(jsonb_typeof(p_batch -> 'since'), 'null') NOT IN ('string', 'null')
    OR COALESCE(jsonb_typeof(p_batch -> 'backfill_before_id'), 'null') NOT IN ('number', 'null')
    OR COALESCE(jsonb_typeof(p_batch -> 'reconcile'), 'null') NOT IN ('object', 'null')
    -- Only a successful pass may reconcile (delete); a failed chat writes no rows.
    OR (v_status <> 'ok' AND (jsonb_typeof(p_batch -> 'reconcile') = 'object' OR jsonb_array_length(p_batch -> 'messages') > 0))
    OR COALESCE(jsonb_typeof(p_batch -> 'retry_after_seconds'), 'null') NOT IN ('number', 'null')
    OR COALESCE((p_batch ->> 'retry_after_seconds')::numeric, 0) NOT BETWEEN 0 AND 86400 THEN
    RAISE EXCEPTION 'invalid Telegram archive batch' USING ERRCODE = '22023';
  END IF;
  v_peer_id := (p_batch ->> 'peer_id')::bigint;

  SELECT metadata INTO v_metadata FROM capability_hub.connections WHERE id = v_connection_id FOR SHARE;
  IF v_metadata IS NOT NULL AND jsonb_typeof(v_metadata #> '{scope,chats}') = 'array' THEN
    SELECT e INTO v_scope_chat FROM jsonb_array_elements(v_metadata #> '{scope,chats}') e WHERE e ->> 'chat_ref' = v_chat_ref LIMIT 1;
  END IF;
  IF v_scope_chat IS NULL THEN
    RETURN jsonb_build_object('status', 'out_of_scope');
  END IF;
  v_since := (v_scope_chat ->> 'since')::date;
  IF (p_batch ->> 'since')::date IS DISTINCT FROM v_since THEN
    RETURN jsonb_build_object('status', 'stale');
  END IF;
  v_since_at := v_since::timestamp AT TIME ZONE 'UTC';

  -- A flood wait is account-wide: the whole Connection waits until Telegram's delay has passed.
  IF v_status = 'flood_wait' THEN
    INSERT INTO telegram_archive.connection_state AS s (connection_id, retry_after)
    VALUES (v_connection_id, clock_timestamp() + make_interval(secs => COALESCE((p_batch ->> 'retry_after_seconds')::integer, 60)))
    ON CONFLICT (connection_id) DO UPDATE SET retry_after = GREATEST(s.retry_after, EXCLUDED.retry_after), updated_at = clock_timestamp();
  END IF;

  SELECT * INTO v_existing FROM telegram_archive.chats WHERE connection_id = v_connection_id AND peer_id = v_peer_id FOR UPDATE;
  IF FOUND THEN
    IF v_existing.chat_ref <> v_chat_ref THEN
      RAISE EXCEPTION 'Telegram archive chat reference mismatch' USING ERRCODE = '22023';
    END IF;
    v_narrowed := v_since IS NOT NULL AND (v_existing.since IS NULL OR v_since > v_existing.since);
  END IF;
  INSERT INTO telegram_archive.chats AS c (connection_id, peer_id, chat_ref, since, state, synced_at, last_message_id, backfill_before_id, gap)
  VALUES (v_connection_id, v_peer_id, v_chat_ref, v_since,
    CASE WHEN v_status = 'inaccessible' THEN 'inaccessible' WHEN (p_batch ->> 'backfill_complete')::boolean THEN 'synced' ELSE 'backfilling' END,
    CASE WHEN v_status = 'ok' THEN clock_timestamp() END,
    (p_batch ->> 'last_message_id')::bigint, (p_batch ->> 'backfill_before_id')::bigint, (p_batch ->> 'gap')::boolean)
  ON CONFLICT (connection_id, peer_id) DO UPDATE SET
    since = EXCLUDED.since,
    state = EXCLUDED.state,
    synced_at = COALESCE(EXCLUDED.synced_at, c.synced_at),
    last_message_id = GREATEST(c.last_message_id, EXCLUDED.last_message_id),
    backfill_before_id = CASE WHEN c.backfill_before_id IS NULL THEN EXCLUDED.backfill_before_id
      WHEN EXCLUDED.backfill_before_id IS NULL THEN c.backfill_before_id
      ELSE LEAST(c.backfill_before_id, EXCLUDED.backfill_before_id) END,
    gap = EXCLUDED.gap,
    updated_at = clock_timestamp();

  -- A later `since` drops the messages now outside it. Only a narrowing scans the chat.
  IF v_narrowed THEN
    DELETE FROM telegram_archive.messages
    WHERE connection_id = v_connection_id AND peer_id = v_peer_id AND sent_at < v_since_at;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_deleted := v_deleted + v_count;
  END IF;

  INSERT INTO telegram_archive.messages AS m (connection_id, peer_id, message_id, author_id, sent_at, edited_at, text, reply_to, media_type, caption)
  SELECT v_connection_id, v_peer_id, r.message_id, r.author_id, r.sent_at, r.edited_at, r.text, r.reply_to, r.media_type, r.caption
  FROM jsonb_to_recordset(p_batch -> 'messages') AS r(message_id bigint, author_id bigint, sent_at timestamptz, edited_at timestamptz,
    text text, reply_to bigint, media_type text, caption text)
  WHERE v_since IS NULL OR r.sent_at >= v_since_at
  ON CONFLICT (connection_id, peer_id, message_id) DO UPDATE SET
    author_id = EXCLUDED.author_id, sent_at = EXCLUDED.sent_at, edited_at = EXCLUDED.edited_at, text = EXCLUDED.text,
    reply_to = EXCLUDED.reply_to, media_type = EXCLUDED.media_type, caption = EXCLUDED.caption,
    -- Changed text invalidates its embedding; T4c-2 re-embeds pending rows.
    embedding = CASE WHEN (m.text, m.caption) IS NOT DISTINCT FROM (EXCLUDED.text, EXCLUDED.caption) THEN m.embedding END,
    embedding_state = CASE WHEN (m.text, m.caption) IS NOT DISTINCT FROM (EXCLUDED.text, EXCLUDED.caption) THEN m.embedding_state ELSE 'pending' END,
    updated_at = clock_timestamp()
  WHERE (m.author_id, m.sent_at, m.edited_at, m.text, m.reply_to, m.media_type, m.caption)
      IS DISTINCT FROM (EXCLUDED.author_id, EXCLUDED.sent_at, EXCLUDED.edited_at, EXCLUDED.text, EXCLUDED.reply_to, EXCLUDED.media_type, EXCLUDED.caption)
    -- An older observation never overwrites a newer edit.
    AND (m.edited_at IS NULL OR (EXCLUDED.edited_at IS NOT NULL AND EXCLUDED.edited_at >= m.edited_at));
  GET DIAGNOSTICS v_written = ROW_COUNT;

  -- After a narrowing the backfill frontier is the oldest message still archived, so a later, wider
  -- `since` resumes backfill from there instead of skipping the dropped range.
  IF v_narrowed THEN
    UPDATE telegram_archive.chats c SET backfill_before_id = COALESCE(
      (SELECT min(message_id) FROM telegram_archive.messages WHERE connection_id = v_connection_id AND peer_id = v_peer_id),
      c.last_message_id + 1)
    WHERE c.connection_id = v_connection_id AND c.peer_id = v_peer_id;
  END IF;

  -- Reconcile: within the re-read window, an archived message Telegram no longer returns was deleted.
  IF jsonb_typeof(p_batch -> 'reconcile') = 'object' THEN
    IF jsonb_typeof(p_batch #> '{reconcile,present_ids}') IS DISTINCT FROM 'array'
      OR jsonb_array_length(p_batch #> '{reconcile,present_ids}') > 1000
      OR COALESCE((p_batch #>> '{reconcile,from_id}')::bigint, 0) < 1
      OR COALESCE((p_batch #>> '{reconcile,to_id}')::bigint, 0) < (p_batch #>> '{reconcile,from_id}')::bigint THEN
      RAISE EXCEPTION 'invalid Telegram archive reconcile window' USING ERRCODE = '22023';
    END IF;
    DELETE FROM telegram_archive.messages
    WHERE connection_id = v_connection_id AND peer_id = v_peer_id
      AND message_id BETWEEN (p_batch #>> '{reconcile,from_id}')::bigint AND (p_batch #>> '{reconcile,to_id}')::bigint
      AND message_id <> ALL (ARRAY(SELECT jsonb_array_elements_text(p_batch #> '{reconcile,present_ids}')::bigint));
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_deleted := v_deleted + v_count;
  END IF;

  RETURN jsonb_build_object('status', 'applied', 'written', v_written, 'deleted', v_deleted);
END $$;

-- Purge on scope removal (CON-TG-06). Fires inside the Connection write itself
-- (`capability_hub.mutate_configuration`), so a chat leaving `metadata.scope.chats` loses its messages,
-- embeddings and progress atomically with the scope change. A removed scope removes every chat.
-- Deleting the Connection cascades through the foreign keys.
CREATE FUNCTION telegram_archive.purge_out_of_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
DECLARE
  v_chats jsonb := NEW.metadata #> '{scope,chats}';
BEGIN
  IF v_chats IS NULL OR jsonb_typeof(v_chats) <> 'array' THEN v_chats := '[]'::jsonb; END IF;
  DELETE FROM telegram_archive.chats c
  WHERE c.connection_id = NEW.id
    AND NOT v_chats @> jsonb_build_array(jsonb_build_object('chat_ref', c.chat_ref));
  RETURN NULL;
END $$;

CREATE TRIGGER telegram_archive_purge_out_of_scope
AFTER UPDATE OF metadata ON capability_hub.connections
FOR EACH ROW WHEN (OLD.metadata #> '{scope,chats}' IS DISTINCT FROM NEW.metadata #> '{scope,chats}')
EXECUTE FUNCTION telegram_archive.purge_out_of_scope();

ALTER TABLE telegram_archive.chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_archive.chats FORCE ROW LEVEL SECURITY;
ALTER TABLE telegram_archive.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_archive.messages FORCE ROW LEVEL SECURITY;
ALTER TABLE telegram_archive.connection_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_archive.connection_state FORCE ROW LEVEL SECURITY;

REVOKE ALL ON SCHEMA telegram_archive FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA telegram_archive FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA telegram_archive FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON SCHEMA telegram_archive FROM anon;
    REVOKE ALL ON ALL TABLES IN SCHEMA telegram_archive FROM anon;
    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA telegram_archive FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON SCHEMA telegram_archive FROM authenticated;
    REVOKE ALL ON ALL TABLES IN SCHEMA telegram_archive FROM authenticated;
    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA telegram_archive FROM authenticated;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT USAGE ON SCHEMA telegram_archive TO service_role;
    GRANT SELECT ON telegram_archive.chats, telegram_archive.messages, telegram_archive.connection_state TO service_role;
    GRANT EXECUTE ON FUNCTION telegram_archive.apply_chat_batch(jsonb) TO service_role;
  END IF;
END
$$;
