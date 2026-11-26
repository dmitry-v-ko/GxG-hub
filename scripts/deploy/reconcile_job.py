# ruff: noqa  (one-off read-only job script, run inside the Approval image by railway_deploy/pilot jobs)
import os, sys, datetime
import psycopg
from pathlib import Path

adsn = Path(os.environ["GXG_SUPERUSER_DSN_FILE"]).read_text().strip()
now = datetime.datetime.now(datetime.timezone.utc)
print("recon time", now.isoformat(), flush=True)
RO = "-c default_transaction_read_only=on"
with psycopg.connect(adsn, autocommit=True, options=RO) as c:
    for st, n in c.execute("SELECT status, count(*) FROM intents GROUP BY 1 ORDER BY 1"):
        print(f"recon intents status={st} n={n}", flush=True)
    for r in c.execute("""SELECT i.intent_id, i.status, i.step_up_required, i.approval_mode, i.tool_key, i.resource_id,
        i.created_at, i.terminal_at, i.attempts FROM intents i ORDER BY i.created_at"""):
        iid, st, su, am, tk, res, cr, term, att = r
        age_h = round((now - cr).total_seconds() / 3600, 2)
        print(
            f"recon intent {iid} status={st} step_up_required={su} approval_mode={am} tool={tk} resource={res} age_h={age_h} terminal={'yes' if term else 'no'} attempts={att}",
            flush=True,
        )
        for d in c.execute(
            "SELECT decision, decided_by, step_up, decided_at, intent_version FROM decisions WHERE intent_id=%s ORDER BY decided_at",
            (iid,),
        ):
            print(
                f"recon   decision {d[0]} by={d[1]} step_up={d[2]} at={d[3].isoformat()} intent_version={d[4]}",
                flush=True,
            )
        for e in c.execute(
            "SELECT attempt, state, provider_operation_id, error_code, completed_at FROM executions WHERE intent_id=%s ORDER BY attempt",
            (iid,),
        ):
            print(
                f"recon   execution attempt={e[0]} state={e[1]} op={e[2]} error={e[3]} completed={e[4].isoformat() if e[4] else None}",
                flush=True,
            )
    for (n,) in c.execute(
        "SELECT count(*) FROM intents i WHERE i.status IN ('succeeded','reconciled_succeeded') AND (SELECT count(*) FROM executions e WHERE e.intent_id=i.intent_id AND e.state IN ('succeeded','reconciled_executed'))<>1"
    ):
        print(f"recon succeeded intents without exactly one successful execution: {n}", flush=True)
    for (n,) in c.execute(
        "SELECT count(*) FROM intents i WHERE i.status IN ('succeeded','reconciled_succeeded') AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.intent_id=i.intent_id AND d.decision='approve')"
    ):
        print(f"recon succeeded intents without an approve decision: {n}", flush=True)
    for (n,) in c.execute(
        "SELECT count(*) FROM executions e WHERE e.state='outcome_unknown' AND e.reserved_at < now() - interval '24 hours'"
    ):
        print(f"recon outcome_unknown executions older than 24h: {n}", flush=True)
    for (n,) in c.execute(
        "SELECT count(*) FROM intents WHERE status='outcome_unknown' AND created_at < now() - interval '24 hours'"
    ):
        print(f"recon outcome_unknown intents older than 24h: {n}", flush=True)
    for (n,) in c.execute(
        "SELECT count(*) FROM (SELECT provider_operation_id FROM executions WHERE provider_operation_id IS NOT NULL GROUP BY 1 HAVING count(*)>1) x"
    ):
        print(f"recon provider_operation_id used by more than one execution: {n}", flush=True)
    for a, n in c.execute("SELECT action, count(*) FROM audit_events GROUP BY 1 ORDER BY 1"):
        print(f"recon audit {a}: {n}", flush=True)
tdsn = os.environ.get("TG_DSN")
if tdsn:
    with psycopg.connect(adsn, autocommit=True, options=RO) as a:
        ops = [
            r
            for r in a.execute(
                "SELECT DISTINCT split_part(provider_operation_id, ':', 3)::bigint, i.resource_id, min(e.completed_at) FROM executions e JOIN intents i USING (intent_id) WHERE provider_operation_id LIKE 'tg:%%' GROUP BY 1, 2"
            )
        ]
    ids = sorted({o[0] for o in ops})
    chats = sorted({o[1].rsplit("/", 1)[-1] for o in ops})
    since = min(o[2] for o in ops if o[2]) - datetime.timedelta(minutes=10)
    print("recon tg ops", ids, "chats", chats, flush=True)
    with psycopg.connect(tdsn, autocommit=True, options=RO) as c:
        for ref in chats:
            for m in c.execute(
                "SELECT m.message_id, m.sent_at, coalesce(length(m.text),0), m.edited_at IS NOT NULL FROM telegram_archive.messages m JOIN telegram_archive.chats ch USING (connection_id, peer_id) WHERE ch.chat_ref=%s AND m.sent_at >= %s ORDER BY m.message_id",
                (ref, since),
            ):
                mark = "SENT-BY-PILOT" if m[0] in ids else "other"
                print(
                    f"recon tg chat={ref} message_id={m[0]} sent_at={m[1].isoformat()} text_len={m[2]} edited={m[3]} {mark}",
                    flush=True,
                )
            n = c.execute(
                "SELECT count(*) FROM telegram_archive.messages m JOIN telegram_archive.chats ch USING (connection_id, peer_id) WHERE ch.chat_ref=%s AND m.message_id = ANY(%s)",
                (ref, ids),
            ).fetchone()[0]
            print(f"recon tg chat={ref} archived messages with a pilot op id: {n} of {len(ids)}", flush=True)
