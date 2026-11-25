-- Read-only role of the Telegram MCP connector on the telegram_archive database (ADR-0004).
-- Run by the database owner after the gxg_agents_v2 archive migrations; the password is set separately
-- (ALTER ROLE gxg_telegram_reader PASSWORD ...) from the secret store, never in this file (SEC-16).
-- BYPASSRLS: the migrations force row level security on every archive table without a policy (only the
-- service role reads them); a policy would change gxg_agents_v2's schema. The role still sees only the
-- columns granted below, and never writes: no INSERT/UPDATE/DELETE, no EXECUTE, read-only transactions.
CREATE ROLE gxg_telegram_reader LOGIN NOINHERIT BYPASSRLS CONNECTION LIMIT 32;
ALTER ROLE gxg_telegram_reader SET default_transaction_read_only = on;
ALTER ROLE gxg_telegram_reader SET statement_timeout = '15s';
REVOKE ALL ON SCHEMA public FROM gxg_telegram_reader;
GRANT USAGE ON SCHEMA telegram_archive TO gxg_telegram_reader;
-- No author_id, no embedding: the connector returns neither (MCC-04).
GRANT SELECT (connection_id, peer_id, chat_ref, state, synced_at, gap) ON telegram_archive.chats TO gxg_telegram_reader;
GRANT SELECT (connection_id, peer_id, message_id, sent_at, edited_at, text, caption, reply_to, media_type, tsv, archived_at)
  ON telegram_archive.messages TO gxg_telegram_reader;
GRANT SELECT (connection_id, retry_after) ON telegram_archive.connection_state TO gxg_telegram_reader;
