-- Telegram message archive (T4c-2): embeddings with `semantic_pending`, and Chat folders read from the archive.
--
-- 1. Embedding pass. A bounded worker claims pending (or failed, after their retry delay) messages that have
--    text or a caption, embeds them through the Signal embedding port and stores the vector only while the
--    message still has the text that was embedded. `embedding_not_before` is both the claim lease and the
--    retry delay of a failed message. Messages without text or caption are never embedded.
-- 2. ANN index. ivfflat with cosine distance, the metric the Signal semantic index uses (`<=>`,
--    `vector_cosine_ops`). `lists = 1` because ivfflat trains its centroids from the rows present at build
--    time and production has no embeddings yet: with more lists, centroids trained on an empty table would
--    send most vectors to arbitrary lists and recall would collapse at `probes = 1`; with one list every
--    vector is in the only list, so recall is exact and the index is safe on an empty or near-empty table.
--    Rebuild with lists = rows / 1000 once the archive holds ~100k embedded messages (pgvector README,
--    "IVFFlat"); the runbook records this.
-- 3. Chat folders. Folder membership is resolved at each sync and stored here; folder members are archived
--    like explicit scope chats (`scope_source = 'folder'`). A chat stays archived while it is an explicit
--    scope chat or a member of a folder in scope; otherwise it is purged with its messages and embeddings,
--    both when the scope changes and when a resolved membership drops it (CON-TG-06).

ALTER TABLE telegram_archive.messages ADD COLUMN embedding_not_before timestamptz;

CREATE INDEX messages_embedding_idx ON telegram_archive.messages
  USING ivfflat (embedding extensions.vector_cosine_ops) WITH (lists = 1);

-- Messages still waiting for an embedding, per chat: drives the claim and `semantic_pending`.
CREATE INDEX messages_embedding_pending_idx ON telegram_archive.messages (connection_id, peer_id)
  WHERE embedding_state <> 'embedded' AND (btrim(coalesce(text, '')) <> '' OR btrim(coalesce(caption, '')) <> '');

ALTER TABLE telegram_archive.chats DROP CONSTRAINT chats_scope_source_check;
ALTER TABLE telegram_archive.chats ADD CONSTRAINT chats_scope_source_check CHECK (scope_source IN ('chat', 'folder'));

-- The last membership resolution of each scope folder. `membership_state` is the latest outcome: `ok`
-- replaced the members; `incomplete` or `too_large` kept the previous members (still archived) but the
-- folder is not current, so Chat folders collection reports it as not synced. `resolved_at` is the last
-- `ok` resolution, if any.
CREATE TABLE telegram_archive.folders (
  connection_id text NOT NULL REFERENCES capability_hub.connections (id) ON DELETE CASCADE,
  folder_ref text NOT NULL CHECK (folder_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  membership_state text NOT NULL CHECK (membership_state IN ('ok', 'incomplete', 'too_large')),
  resolved_at timestamptz,
  checked_at timestamptz NOT NULL,
  PRIMARY KEY (connection_id, folder_ref),
  CHECK (membership_state <> 'ok' OR resolved_at IS NOT NULL)
);

CREATE TABLE telegram_archive.folder_members (
  connection_id text NOT NULL,
  folder_ref text NOT NULL,
  peer_id bigint NOT NULL,
  PRIMARY KEY (connection_id, folder_ref, peer_id),
  FOREIGN KEY (connection_id, folder_ref) REFERENCES telegram_archive.folders (connection_id, folder_ref) ON DELETE CASCADE
);

CREATE INDEX folder_members_peer_idx ON telegram_archive.folder_members (connection_id, peer_id);

COMMENT ON TABLE telegram_archive.folders IS 'Latest membership resolution outcome of each Telegram Connection scope folder.';
COMMENT ON TABLE telegram_archive.folder_members IS 'Resolved members of Telegram Connection scope folders; archived like scope chats.';

-- Removes what is no longer in a Connection's scope: memberships of folders that left `scope.folders`, then
-- every archived chat that is neither in `scope.chats` nor a member of a remaining folder. Messages and
-- embeddings go with their chat.
CREATE FUNCTION telegram_archive.purge_unscoped(p_connection_id text, p_metadata jsonb) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
DECLARE
  v_chats jsonb := p_metadata #> '{scope,chats}';
  v_folders jsonb := p_metadata #> '{scope,folders}';
  v_count integer;
BEGIN
  IF v_chats IS NULL OR jsonb_typeof(v_chats) <> 'array' THEN v_chats := '[]'::jsonb; END IF;
  IF v_folders IS NULL OR jsonb_typeof(v_folders) <> 'array' THEN v_folders := '[]'::jsonb; END IF;
  DELETE FROM telegram_archive.folders f
  WHERE f.connection_id = p_connection_id AND NOT v_folders @> jsonb_build_array(jsonb_build_object('folder_ref', f.folder_ref));
  DELETE FROM telegram_archive.chats c
  WHERE c.connection_id = p_connection_id
    AND NOT v_chats @> jsonb_build_array(jsonb_build_object('chat_ref', c.chat_ref))
    AND NOT EXISTS (SELECT 1 FROM telegram_archive.folder_members fm WHERE fm.connection_id = c.connection_id AND fm.peer_id = c.peer_id);
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END $$;

-- Purge on scope removal (CON-TG-06), now covering folders: fires inside the Connection write itself.
CREATE OR REPLACE FUNCTION telegram_archive.purge_out_of_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
BEGIN
  PERFORM telegram_archive.purge_unscoped(NEW.id, NEW.metadata);
  RETURN NULL;
END $$;

DROP TRIGGER telegram_archive_purge_out_of_scope ON capability_hub.connections;
CREATE TRIGGER telegram_archive_purge_out_of_scope
AFTER UPDATE OF metadata ON capability_hub.connections
FOR EACH ROW WHEN (OLD.metadata #> '{scope,chats}' IS DISTINCT FROM NEW.metadata #> '{scope,chats}'
  OR OLD.metadata #> '{scope,folders}' IS DISTINCT FROM NEW.metadata #> '{scope,folders}')
EXECUTE FUNCTION telegram_archive.purge_out_of_scope();

-- One resolved folder membership from `archive-sync`, applied atomically with the purge of the chats it
-- drops. The Connection row is locked FOR NO KEY UPDATE, which conflicts with the FOR SHARE lock every
-- `apply_chat_batch` takes: a membership change and a member's batch never interleave, so a batch cannot
-- write a chat this membership is purging, and foreign-key inserts (FOR KEY SHARE) are not blocked. The
-- folder must still be in scope. Only `ok` replaces the membership; an incomplete or oversized resolution
-- keeps the previous members but marks the folder not current. A flood wait defers the Connection like a
-- chat batch does and leaves the folder as it was.
CREATE FUNCTION telegram_archive.apply_folder_membership(p_folder jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
DECLARE
  v_connection_id text := p_folder ->> 'connection_id';
  v_folder_ref text := p_folder ->> 'folder_ref';
  v_status text := p_folder ->> 'status';
  v_metadata jsonb;
  v_members bigint[];
  v_purged integer;
BEGIN
  IF jsonb_typeof(p_folder) IS DISTINCT FROM 'object' OR v_connection_id IS NULL
    OR COALESCE(v_folder_ref, '') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    OR v_status IS NULL OR v_status NOT IN ('ok', 'incomplete', 'too_large', 'flood_wait')
    OR jsonb_typeof(p_folder -> 'members') IS DISTINCT FROM 'array' OR jsonb_array_length(p_folder -> 'members') > 500
    OR (v_status <> 'ok' AND jsonb_array_length(p_folder -> 'members') > 0)
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_folder -> 'members') e WHERE jsonb_typeof(e) <> 'string' OR e #>> '{}' !~ '^-?[0-9]{1,19}$')
    OR COALESCE(jsonb_typeof(p_folder -> 'retry_after_seconds'), 'null') NOT IN ('number', 'null')
    OR COALESCE((p_folder ->> 'retry_after_seconds')::numeric, 0) NOT BETWEEN 0 AND 86400 THEN
    RAISE EXCEPTION 'invalid Telegram archive folder membership' USING ERRCODE = '22023';
  END IF;

  SELECT metadata INTO v_metadata FROM capability_hub.connections WHERE id = v_connection_id FOR NO KEY UPDATE;
  IF v_metadata IS NULL OR jsonb_typeof(v_metadata #> '{scope,folders}') IS DISTINCT FROM 'array'
    OR NOT (v_metadata #> '{scope,folders}') @> jsonb_build_array(jsonb_build_object('folder_ref', v_folder_ref)) THEN
    RETURN jsonb_build_object('status', 'out_of_scope');
  END IF;

  IF v_status = 'flood_wait' THEN
    INSERT INTO telegram_archive.connection_state AS s (connection_id, retry_after)
    VALUES (v_connection_id, clock_timestamp() + make_interval(secs => COALESCE((p_folder ->> 'retry_after_seconds')::integer, 60)))
    ON CONFLICT (connection_id) DO UPDATE SET retry_after = GREATEST(s.retry_after, EXCLUDED.retry_after), updated_at = clock_timestamp();
    RETURN jsonb_build_object('status', 'unchanged');
  END IF;
  IF v_status <> 'ok' THEN
    INSERT INTO telegram_archive.folders AS f (connection_id, folder_ref, membership_state, checked_at)
    VALUES (v_connection_id, v_folder_ref, v_status, clock_timestamp())
    ON CONFLICT (connection_id, folder_ref) DO UPDATE SET membership_state = EXCLUDED.membership_state, checked_at = EXCLUDED.checked_at;
    RETURN jsonb_build_object('status', 'unchanged');
  END IF;

  v_members := ARRAY(SELECT DISTINCT (e #>> '{}')::bigint FROM jsonb_array_elements(p_folder -> 'members') e);
  INSERT INTO telegram_archive.folders AS f (connection_id, folder_ref, membership_state, resolved_at, checked_at)
  VALUES (v_connection_id, v_folder_ref, 'ok', clock_timestamp(), clock_timestamp())
  ON CONFLICT (connection_id, folder_ref) DO UPDATE
    SET membership_state = 'ok', resolved_at = EXCLUDED.resolved_at, checked_at = EXCLUDED.checked_at;
  DELETE FROM telegram_archive.folder_members
  WHERE connection_id = v_connection_id AND folder_ref = v_folder_ref AND peer_id <> ALL (v_members);
  INSERT INTO telegram_archive.folder_members (connection_id, folder_ref, peer_id)
  SELECT v_connection_id, v_folder_ref, peer FROM unnest(v_members) AS peer
  ON CONFLICT DO NOTHING;
  v_purged := telegram_archive.purge_unscoped(v_connection_id, v_metadata);
  RETURN jsonb_build_object('status', 'applied', 'members', cardinality(v_members), 'purged', v_purged);
END $$;

-- One chat batch from `archive-sync` (T4c-1 contract), now admitting folder members. A chat is in scope
-- when it is an explicit scope chat or a member of a scope folder; its `since` is the earliest of those
-- sources (no `since` on any source means none), taken from the locked scope. A batch computed for another
-- `since` is stale.
CREATE OR REPLACE FUNCTION telegram_archive.apply_chat_batch(p_batch jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
DECLARE
  v_connection_id text := p_batch ->> 'connection_id';
  v_chat_ref text := p_batch ->> 'chat_ref';
  v_peer_id bigint;
  v_status text := p_batch ->> 'status';
  v_metadata jsonb;
  v_scope_chat jsonb;
  v_scope_folders jsonb;
  v_sources integer;
  v_any_unbounded boolean;
  v_min_since date;
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
  v_scope_folders := CASE WHEN jsonb_typeof(v_metadata #> '{scope,folders}') = 'array' THEN v_metadata #> '{scope,folders}' ELSE '[]'::jsonb END;
  SELECT count(*), COALESCE(bool_or(source.since IS NULL), false), min(source.since::date)
  INTO v_sources, v_any_unbounded, v_min_since
  FROM (
    SELECT v_scope_chat ->> 'since' AS since WHERE v_scope_chat IS NOT NULL
    UNION ALL
    SELECT f ->> 'since' FROM jsonb_array_elements(v_scope_folders) f
    WHERE EXISTS (SELECT 1 FROM telegram_archive.folder_members fm
      WHERE fm.connection_id = v_connection_id AND fm.folder_ref = f ->> 'folder_ref' AND fm.peer_id = v_peer_id)
  ) source;
  IF v_sources = 0 THEN
    RETURN jsonb_build_object('status', 'out_of_scope');
  END IF;
  v_since := CASE WHEN v_any_unbounded THEN NULL ELSE v_min_since END;
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
  INSERT INTO telegram_archive.chats AS c (connection_id, peer_id, chat_ref, scope_source, since, state, synced_at, last_message_id, backfill_before_id, gap)
  VALUES (v_connection_id, v_peer_id, v_chat_ref, CASE WHEN v_scope_chat IS NOT NULL THEN 'chat' ELSE 'folder' END, v_since,
    CASE WHEN v_status = 'inaccessible' THEN 'inaccessible' WHEN (p_batch ->> 'backfill_complete')::boolean THEN 'synced' ELSE 'backfilling' END,
    CASE WHEN v_status = 'ok' THEN clock_timestamp() END,
    (p_batch ->> 'last_message_id')::bigint, (p_batch ->> 'backfill_before_id')::bigint, (p_batch ->> 'gap')::boolean)
  ON CONFLICT (connection_id, peer_id) DO UPDATE SET
    scope_source = EXCLUDED.scope_source,
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
    -- Changed text invalidates its embedding and makes it claimable again at once.
    embedding = CASE WHEN (m.text, m.caption) IS NOT DISTINCT FROM (EXCLUDED.text, EXCLUDED.caption) THEN m.embedding END,
    embedding_state = CASE WHEN (m.text, m.caption) IS NOT DISTINCT FROM (EXCLUDED.text, EXCLUDED.caption) THEN m.embedding_state ELSE 'pending' END,
    embedding_not_before = CASE WHEN (m.text, m.caption) IS NOT DISTINCT FROM (EXCLUDED.text, EXCLUDED.caption) THEN m.embedding_not_before END,
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

-- Claims up to `p_limit` messages to embed: pending ones, and failed ones whose retry delay has passed. The
-- claim is a lease of `p_lease_seconds`, so an overlapping pass skips them. `content_hash` pins the text
-- that is embedded; a vector is stored only while the message still has that text.
CREATE FUNCTION telegram_archive.claim_embedding_batch(p_limit integer, p_lease_seconds integer)
RETURNS TABLE (connection_id text, peer_id text, message_id bigint, content text, content_hash text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
  WITH picked AS (
    SELECT m.connection_id, m.peer_id, m.message_id
    FROM telegram_archive.messages m
    WHERE m.embedding_state <> 'embedded' AND (btrim(coalesce(m.text, '')) <> '' OR btrim(coalesce(m.caption, '')) <> '')
      AND (m.embedding_not_before IS NULL OR m.embedding_not_before <= clock_timestamp())
      AND p_limit BETWEEN 1 AND 100 AND p_lease_seconds BETWEEN 1 AND 3600
    ORDER BY m.embedding_not_before NULLS FIRST, m.archived_at, m.connection_id, m.peer_id, m.message_id
    LIMIT p_limit
    FOR UPDATE OF m SKIP LOCKED
  )
  UPDATE telegram_archive.messages m
  SET embedding_not_before = clock_timestamp() + make_interval(secs => p_lease_seconds)
  FROM picked
  WHERE m.connection_id = picked.connection_id AND m.peer_id = picked.peer_id AND m.message_id = picked.message_id
  RETURNING m.connection_id, m.peer_id::text, m.message_id,
    concat_ws(E'\n', nullif(btrim(coalesce(m.text, '')), ''), nullif(btrim(coalesce(m.caption, '')), '')),
    md5(coalesce(m.text, '') || E'\x01' || coalesce(m.caption, ''));
$$;

-- Stores claimed vectors. A message whose text changed since its claim keeps no vector and stays pending.
CREATE FUNCTION telegram_archive.store_embeddings(p_items jsonb) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
DECLARE
  v_count integer;
BEGIN
  IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) > 100
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_items) e WHERE jsonb_typeof(e) <> 'object'
      OR COALESCE(e ->> 'peer_id', '') !~ '^-?[0-9]{1,19}$' OR jsonb_typeof(e -> 'message_id') IS DISTINCT FROM 'number'
      OR COALESCE(e ->> 'content_hash', '') !~ '^[0-9a-f]{32}$' OR jsonb_typeof(e -> 'embedding') IS DISTINCT FROM 'string'
      OR e ->> 'connection_id' IS NULL) THEN
    RAISE EXCEPTION 'invalid Telegram archive embeddings' USING ERRCODE = '22023';
  END IF;
  -- Lock the rows in message-key order first, so concurrent writers of the same messages queue instead of deadlocking.
  PERFORM 1 FROM telegram_archive.messages m
  JOIN (SELECT DISTINCT e ->> 'connection_id' AS connection_id, (e ->> 'peer_id')::bigint AS peer_id, (e ->> 'message_id')::bigint AS message_id
        FROM jsonb_array_elements(p_items) e) k
    ON m.connection_id = k.connection_id AND m.peer_id = k.peer_id AND m.message_id = k.message_id
  ORDER BY m.connection_id, m.peer_id, m.message_id
  FOR UPDATE OF m;
  UPDATE telegram_archive.messages m
  SET embedding = (e ->> 'embedding')::extensions.vector(1536), embedding_state = 'embedded', embedding_not_before = NULL
  FROM jsonb_array_elements(p_items) e
  WHERE m.connection_id = e ->> 'connection_id' AND m.peer_id = (e ->> 'peer_id')::bigint AND m.message_id = (e ->> 'message_id')::bigint
    AND m.embedding_state <> 'embedded'
    AND md5(coalesce(m.text, '') || E'\x01' || coalesce(m.caption, '')) = e ->> 'content_hash';
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END $$;

-- Marks claimed messages failed; they are claimable again after `p_retry_after_seconds` (the retry path).
CREATE FUNCTION telegram_archive.mark_embeddings_failed(p_items jsonb, p_retry_after_seconds integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, telegram_archive AS $$
DECLARE
  v_count integer;
BEGIN
  IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) > 100
    OR p_retry_after_seconds IS NULL OR p_retry_after_seconds NOT BETWEEN 1 AND 86400
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_items) e WHERE jsonb_typeof(e) <> 'object'
      OR COALESCE(e ->> 'peer_id', '') !~ '^-?[0-9]{1,19}$' OR jsonb_typeof(e -> 'message_id') IS DISTINCT FROM 'number'
      OR e ->> 'connection_id' IS NULL) THEN
    RAISE EXCEPTION 'invalid Telegram archive embedding failure' USING ERRCODE = '22023';
  END IF;
  -- Lock the rows in message-key order first, so concurrent writers of the same messages queue instead of deadlocking.
  PERFORM 1 FROM telegram_archive.messages m
  JOIN (SELECT DISTINCT e ->> 'connection_id' AS connection_id, (e ->> 'peer_id')::bigint AS peer_id, (e ->> 'message_id')::bigint AS message_id
        FROM jsonb_array_elements(p_items) e) k
    ON m.connection_id = k.connection_id AND m.peer_id = k.peer_id AND m.message_id = k.message_id
  ORDER BY m.connection_id, m.peer_id, m.message_id
  FOR UPDATE OF m;
  UPDATE telegram_archive.messages m
  SET embedding_state = 'failed', embedding = NULL, embedding_not_before = clock_timestamp() + make_interval(secs => p_retry_after_seconds)
  FROM jsonb_array_elements(p_items) e
  WHERE m.connection_id = e ->> 'connection_id' AND m.peer_id = (e ->> 'peer_id')::bigint AND m.message_id = (e ->> 'message_id')::bigint
    AND m.embedding_state <> 'embedded';
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END $$;

ALTER TABLE telegram_archive.folders ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_archive.folders FORCE ROW LEVEL SECURITY;
ALTER TABLE telegram_archive.folder_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_archive.folder_members FORCE ROW LEVEL SECURITY;

REVOKE ALL ON telegram_archive.folders, telegram_archive.folder_members FROM PUBLIC;
REVOKE ALL ON FUNCTION telegram_archive.purge_unscoped(text, jsonb), telegram_archive.apply_folder_membership(jsonb),
  telegram_archive.claim_embedding_batch(integer, integer), telegram_archive.store_embeddings(jsonb),
  telegram_archive.mark_embeddings_failed(jsonb, integer) FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA telegram_archive FROM anon;
    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA telegram_archive FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA telegram_archive FROM authenticated;
    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA telegram_archive FROM authenticated;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT SELECT ON telegram_archive.folders, telegram_archive.folder_members TO service_role;
    GRANT EXECUTE ON FUNCTION telegram_archive.apply_folder_membership(jsonb), telegram_archive.claim_embedding_batch(integer, integer),
      telegram_archive.store_embeddings(jsonb), telegram_archive.mark_embeddings_failed(jsonb, integer) TO service_role;
  END IF;
END
$$;
