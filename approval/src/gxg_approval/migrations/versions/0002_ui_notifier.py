"""M9: Telegram link codes, passkeys, narrowing of gxg_ui (AS-27).

AS-27 has no table for either: the notifier learns `(code, telegram_user_id)` from `/link <code>` but may only
SELECT approver_links, and AS-17 needs registered passkeys. Both tables are a spec gap recorded as
`pending_owner_decision` (reports/M9-ESCALATION.md). The notifier writes approver_links only through
gxg_link_claim; the ui creates codes only through gxg_link_code.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"

DEFINER = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp"
CODE_TTL_S = 600  # «Привязка Telegram»: 6 digits, 10 minutes, once
MAX_FAILED_CLAIMS = 5  # per Telegram user per 10 minutes: 10^6 codes cannot be walked through


def upgrade() -> None:
    op.execute(f"""
CREATE TABLE link_codes (
  id bigserial PRIMARY KEY,
  code_hash char(64) NOT NULL,          -- sha256 of the 6 digits; the code itself is never stored
  human_principal text NOT NULL,
  tenant_ids text[] NOT NULL,           -- tenants where the human is an approver, by the bundle at creation
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  telegram_user_id bigint
);
CREATE INDEX ON link_codes (code_hash) WHERE used_at IS NULL;

CREATE TABLE webauthn_credentials (
  credential_id bytea PRIMARY KEY,
  human_principal text NOT NULL,
  public_key bytea NOT NULL,            -- COSE key from the registration (AS-16, AS-17)
  sign_count bigint NOT NULL,
  created_at timestamptz NOT NULL
);
CREATE INDEX ON webauthn_credentials (human_principal);

CREATE FUNCTION _gxg_code_hash(p_code text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(sha256(convert_to(p_code, 'UTF8')), 'hex')
$$;

-- ui: a new code for a human; the human's earlier unused codes stop working. false: the digits collide with a
-- live code of someone else (the ui draws new digits).
CREATE FUNCTION gxg_link_code(p_human text, p_tenants text[], p_code text) RETURNS boolean {DEFINER} AS $$
BEGIN
  IF NOT p_human LIKE 'oidc:%' OR p_code !~ '^[0-9]{{6}}$' OR coalesce(cardinality(p_tenants), 0) = 0 THEN
    PERFORM _gxg_fail('GX400', 'a human, six digits and at least one tenant');
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('gxg:link', 0));  -- one live code per digits
  IF EXISTS (SELECT 1 FROM link_codes WHERE code_hash = _gxg_code_hash(p_code) AND used_at IS NULL
             AND expires_at > now()) THEN
    RETURN false;
  END IF;
  UPDATE link_codes SET expires_at = now() WHERE human_principal = p_human AND used_at IS NULL AND expires_at > now();
  INSERT INTO link_codes (code_hash, human_principal, tenant_ids, created_at, expires_at)
  VALUES (_gxg_code_hash(p_code), p_human, p_tenants, now(), now() + make_interval(secs => {CODE_TTL_S}));
  INSERT INTO audit_events (at, actor, action, details)
  VALUES (now(), p_human, 'telegram_link_code', jsonb_build_object('tenants', p_tenants));
  RETURN true;
END $$;

-- notifier: `/link <code>` from a Telegram user. Returns the linked human, NULL when the code is unknown,
-- expired or used. A refused claim is audited and counts against the sender.
CREATE FUNCTION gxg_link_claim(p_code text, p_telegram_user bigint) RETURNS text {DEFINER} AS $$
DECLARE c link_codes; t text;
BEGIN
  IF (SELECT count(*) FROM audit_events WHERE action = 'telegram_link_refused' AND at > now() - interval '10 minutes'
        AND details->>'telegram_user_id' = p_telegram_user::text) >= {MAX_FAILED_CLAIMS} THEN
    RETURN NULL;
  END IF;
  SELECT * INTO c FROM link_codes WHERE code_hash = _gxg_code_hash(coalesce(p_code, '')) AND used_at IS NULL
                                     AND expires_at > now() FOR UPDATE;
  IF NOT FOUND THEN
    INSERT INTO audit_events (at, actor, action, details)
    VALUES (now(), 'notifier', 'telegram_link_refused', jsonb_build_object('telegram_user_id', p_telegram_user));
    RETURN NULL;
  END IF;
  UPDATE link_codes SET used_at = now(), telegram_user_id = p_telegram_user WHERE id = c.id;
  FOREACH t IN ARRAY c.tenant_ids LOOP
    INSERT INTO approver_links (human_principal, tenant_id, telegram_user_id, linked_at)
    VALUES (c.human_principal, t, p_telegram_user, now())
    ON CONFLICT (human_principal, tenant_id) DO UPDATE SET telegram_user_id = excluded.telegram_user_id,
                                                           linked_at = excluded.linked_at;
  END LOOP;
  INSERT INTO audit_events (at, actor, action, details)
  VALUES (now(), c.human_principal, 'telegram_linked',
          jsonb_build_object('telegram_user_id', p_telegram_user, 'tenants', c.tenant_ids));
  RETURN c.human_principal;
END $$;
-- No PUBLIC rights: tables have none by default, functions by 0001's ALTER DEFAULT PRIVILEGES.
""")
    op.execute("""
GRANT SELECT, INSERT, UPDATE (sign_count) ON webauthn_credentials TO gxg_ui;
GRANT EXECUTE ON FUNCTION gxg_link_code(text, text[], text) TO gxg_ui;
GRANT EXECUTE ON FUNCTION gxg_link_claim(text, bigint) TO gxg_notifier;
-- Narrowing of AS-27 for gxg_ui: every write of the ui goes through gxg_decide / gxg_resolve_manual /
-- gxg_link_code (and the passkey table); it needs no outbox row and no direct approver_links write.
REVOKE INSERT ON outbox FROM gxg_ui;
REVOKE INSERT, UPDATE ON approver_links FROM gxg_ui;
REVOKE USAGE ON SEQUENCE outbox_id_seq FROM gxg_ui;
""")


def downgrade() -> None:
    raise NotImplementedError("no downgrade: audit and intents are never dropped (AS-02, RET-02)")
