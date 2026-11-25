"""AC-M10-08 (AS-30) on the decider's own API: a decision without a valid assertion, with an assertion for another
intent, args hash, version or decision, a replayed one, or one by a passkey not enrolled through the decider → refused,
no row in `decisions`. The ui-level flow is in approval/ui/tests_db/test_decisions.py."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, parse_registration_credential_json
from webauthn_soft import PUBLIC_URL, SoftAuthenticator

from gxg_approval import decider

HUMAN1, HUMAN2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"
OPERATOR = "oidc:https://idp.example.invalid:operator"


@pytest.fixture
def api(pg: Any, owner: psycopg.Connection[Any]) -> Iterator[TestClient]:
    with ConnectionPool(pg.dsn("gxg_decider"), min_size=1, max_size=2, kwargs={"autocommit": True}) as pool:
        app = decider.create_app(decider.Deps(pool, rp_id="ui.example.test", origin=PUBLIC_URL))
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


@pytest.fixture
def enroll(api: TestClient, pg: Any) -> Callable[..., SoftAuthenticator]:
    def make(human: str = HUMAN1, key: SoftAuthenticator | None = None) -> SoftAuthenticator:
        key = key or SoftAuthenticator()
        token = secrets.token_urlsafe(24)
        with pg.connect("gxg_admin") as admin:
            admin.execute("SELECT gxg_passkey_invite(%s, %s, %s, 600)", (human, decider.invite_hash(token), OPERATOR))
        options = api.post("/v1/passkeys/options", json={"human": human, "invite": token})
        assert options.status_code == 200, options.text
        r = api.post("/v1/passkeys", json={"human": human, "invite": token, "credential": key.register(options.text)})
        assert r.status_code == 200, r.text
        return key

    return make


def intent(propose: Callable[..., Any], owner: psycopg.Connection[Any], **kw: Any) -> dict[str, Any]:
    created = propose(**kw).json()
    row = owner.execute(
        "SELECT version, canonical_args_hash FROM intents WHERE intent_id = %s", (created["intent_id"],)
    ).fetchone()
    assert row is not None
    return {"intent_id": created["intent_id"], "version": row[0], "args_hash": row[1]}


def options(api: TestClient, i: dict[str, Any], human: str = HUMAN1, decision: str = "approve") -> Any:
    return api.post("/v1/decisions/options", json={"human": human, "decision": decision, **i})


def decide(api: TestClient, i: dict[str, Any], assertion: str, human: str = HUMAN1) -> Any:
    return api.post(
        "/v1/decisions", json={"human": human, "intent_id": i["intent_id"], "assertion": assertion, "comment": "ok"}
    )


def count(owner: psycopg.Connection[Any]) -> int:
    return int((owner.execute("SELECT count(*) FROM decisions").fetchone() or (0,))[0])


def test_signed_decision_is_recorded_with_its_assertion(
    api: TestClient, enroll: Any, propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    key = enroll()
    i = intent(propose, owner)
    opts = json.loads(options(api, i).text)
    assert opts["rpId"] == "ui.example.test" and opts["userVerification"] == "required"
    raw = base64url_to_bytes(opts["challenge"])
    bound = f"{i['intent_id']}{i['args_hash']}{i['version']}approve".encode()
    assert raw[:32] == hashlib.sha256(bound).digest() and len(raw) == 48
    r = decide(api, i, key.assertion(options(api, i).text))
    assert r.status_code == 200 and r.json() == {"decided": True}
    row = owner.execute("SELECT decided_by, decision, step_up, comment, webauthn_assertion FROM decisions").fetchone()
    assert row is not None and row[:4] == (HUMAN1, "approve", True, "ok")
    stored = row[4]
    assert set(stored) == {"credential_id", "authenticator_data", "client_data_json", "signature"}
    client = json.loads(base64url_to_bytes(stored["client_data_json"]))
    assert base64url_to_bytes(client["challenge"])[:32] == hashlib.sha256(bound).digest()  # verifiable later
    assert owner.execute("SELECT sign_count FROM webauthn_credentials").fetchone() == (key.count,)


def test_refusals_write_nothing(
    api: TestClient, enroll: Any, propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    key = enroll()
    a, b = intent(propose, owner), intent(propose, owner, args={"text": "b", "n": 2})
    good = options(api, a).text
    cases = {
        "no assertion": "",
        "not JSON": "{",
        "empty object": "{}",
        "for another intent": key.assertion(options(api, b).text),
        "another origin": key.assertion(good, origin="https://evil.example"),
        "unknown challenge": key.assertion('{"challenge": "' + bytes_to_base64url(b"x" * 48) + '"}'),
    }
    key.user_verified = False
    cases["no user verification"] = key.assertion(good)
    key.user_verified = True
    for name, assertion in cases.items():
        r = decide(api, a, assertion)
        assert r.status_code == 403 and r.json() == {"message": "assertion refused"}, name
    assert count(owner) == 0


def test_named_human_must_own_the_passkey(
    api: TestClient, enroll: Any, propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    """A compromised ui names another approver: the challenge and the credential bind the human."""
    key1, _ = enroll(HUMAN1), enroll(HUMAN2)
    i = intent(propose, owner)
    signed = key1.assertion(options(api, i).text)  # HUMAN1's challenge, HUMAN1's passkey
    assert decide(api, i, signed, human=HUMAN2).status_code == 403
    signed2 = key1.assertion(options(api, i, human=HUMAN2).text)  # HUMAN2's challenge, HUMAN1's passkey
    assert decide(api, i, signed2, human=HUMAN2).status_code == 403
    assert count(owner) == 0


def test_replay_and_stale_version(
    api: TestClient, enroll: Any, propose: Callable[..., Any], owner: psycopg.Connection[Any], world: Any
) -> None:
    world.set_policy("t-alpha", [{"id": "two", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "require_approval", "approvals": 2}])  # fmt: skip
    key1, key2 = enroll(HUMAN1, SoftAuthenticator(zero_counter=True)), enroll(HUMAN2)
    i = intent(propose, owner)
    first = key1.assertion(options(api, i).text)
    late = key2.assertion(options(api, i, human=HUMAN2).text)  # signed for version 1
    assert decide(api, i, first).status_code == 200
    assert decide(api, i, first).status_code == 403  # the challenge is used
    assert decide(api, i, late, human=HUMAN2).status_code == 409  # version moved to 2
    assert count(owner) == 1
    assert options(api, i, human=HUMAN2).status_code == 409  # a challenge for the old version is not even issued


def test_signed_decision_is_the_one_recorded(
    api: TestClient, enroll: Any, propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    key = enroll()
    i = intent(propose, owner)
    rejected = key.assertion(options(api, i, decision="reject").text)
    assert decide(api, i, rejected).status_code == 200
    assert owner.execute("SELECT decision FROM decisions").fetchall() == [("reject",)]


def test_passkey_written_around_the_decider_is_refused(
    api: TestClient, enroll: Any, propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    enroll()  # an enrolled one exists, so options are issued
    rogue = SoftAuthenticator()
    reg = json.loads(rogue.register('{"challenge": "AAAA"}'))
    att = parse_registration_credential_json(json.dumps(reg))
    from webauthn.helpers import parse_attestation_object

    data = parse_attestation_object(att.response.attestation_object).auth_data.attested_credential_data
    assert data is not None
    owner.execute(
        "INSERT INTO webauthn_credentials (credential_id, human_principal, public_key, sign_count, created_at) "
        "VALUES (%s, %s, %s, 0, now())",
        (rogue.credential_id, HUMAN1, data.credential_public_key),
    )
    i = intent(propose, owner)
    assert decide(api, i, rogue.assertion(options(api, i).text)).status_code == 403
    allow = json.loads(options(api, i).text)["allowCredentials"]
    assert bytes_to_base64url(rogue.credential_id) not in {c["id"] for c in allow}
    assert count(owner) == 0


def test_options_need_an_enrolled_passkey_and_a_valid_request(
    api: TestClient, propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    i = intent(propose, owner)
    assert options(api, i).status_code == 403  # AS-17: nothing enrolled
    assert options(api, i, decision="maybe").status_code == 400
    assert options(api, i, human="agent:t-alpha:bot").status_code == 403
    assert options(api, {**i, "intent_id": "00000000-0000-4000-8000-000000000001"}).status_code == 404
    assert api.post("/v1/decisions/options", json={"human": HUMAN1}).status_code == 422


def test_enrollment_refusals(api: TestClient, pg: Any, owner: psycopg.Connection[Any]) -> None:
    token = secrets.token_urlsafe(24)
    with pg.connect("gxg_admin") as admin:
        admin.execute("SELECT gxg_passkey_invite(%s, %s, %s, 600)", (HUMAN1, decider.invite_hash(token), OPERATOR))
    key = SoftAuthenticator()
    assert api.post("/v1/passkeys/options", json={"human": HUMAN2, "invite": token}).status_code == 403
    opts = api.post("/v1/passkeys/options", json={"human": HUMAN1, "invite": token}).text
    other = secrets.token_urlsafe(24)
    for human, invite, credential in (
        (HUMAN2, token, key.register(opts)),  # another human
        (HUMAN1, other, key.register(opts)),  # another invite
        (HUMAN1, token, "{"),  # malformed
        (HUMAN1, token, SoftAuthenticator(origin="https://evil.example").register(opts)),
        (HUMAN1, token, SoftAuthenticator(user_verified=False).register(opts)),
    ):
        r = api.post("/v1/passkeys", json={"human": human, "invite": invite, "credential": credential})
        assert r.status_code == 403, (human, invite)
    ok = api.post("/v1/passkeys", json={"human": HUMAN1, "invite": token, "credential": key.register(opts)})
    assert ok.status_code == 200
    again = {"human": HUMAN1, "invite": token, "credential": key.register(opts)}
    assert api.post("/v1/passkeys", json=again).status_code == 403  # the challenge and the invite are used
    rows = owner.execute("SELECT human_principal, enrolled_by FROM webauthn_credentials").fetchall()
    assert rows == [(HUMAN1, OPERATOR)]
    audit = owner.execute("SELECT actor, details FROM audit_events WHERE action = 'passkey_enrolled'").fetchall()
    assert audit == [(HUMAN1, {"invited_by": OPERATOR})]


def test_health(api: TestClient, pg: Any) -> None:
    assert api.get("/healthz").json() == {"status": "ok"}
    assert api.get("/readyz").status_code == 200
    with ConnectionPool(pg.dsn("gxg_decider"), min_size=0, max_size=1, open=True) as pool:
        not_ready = decider.create_app(decider.Deps(pool, "ui.example.test", PUBLIC_URL, ready=lambda: False))
        with TestClient(not_ready) as client:
            assert client.get("/readyz").status_code == 503


def test_database_functions_refuse_on_their_own(pg: Any, propose: Callable[..., Any], owner: Any) -> None:
    """Defence in depth: gxg_decide itself refuses what the decider's Python already filters (a compromised decider
    process or a bug there): unknown, used or old challenge, a passkey not enrolled or of another human, a counter
    that did not grow, an assertion without its parts."""
    i = intent(propose, owner)
    full = '{"credential_id": "c", "authenticator_data": "a", "client_data_json": "j", "signature": "s"}'
    owner.execute(
        "INSERT INTO webauthn_credentials VALUES ('\\x01', %s, '\\x00', 5, now(), %s), "
        "('\\x02', %s, '\\x00', 0, now(), NULL), ('\\x03', %s, '\\x00', 0, now(), %s)",
        (HUMAN1, OPERATOR, HUMAN1, HUMAN2, OPERATOR),
    )

    def challenge(conn: Any) -> bytes:
        row = conn.execute(
            "SELECT gxg_decision_challenge(%s, %s, 'approve', %s, %s)", (HUMAN1, i["intent_id"], 1, i["args_hash"])
        ).fetchone()
        return bytes(row[0])

    with pg.connect("gxg_decider") as d:
        cases = [
            (b"\x00" * 48, b"\x01", 6, full, "GX403"),  # unknown challenge
            (None, b"\x02", 6, full, "GX403"),  # not enrolled through the decider
            (None, b"\x03", 6, full, "GX403"),  # HUMAN2's passkey for HUMAN1's challenge
            (None, b"\x01", 5, full, "GX403"),  # the counter did not grow
            (None, b"\x01", 6, '{"credential_id": "c"}', "GX400"),  # assertion without its parts
            (None, b"\x01", 6, '"text"', "GX400"),
        ]
        for ch, cred, n, assertion, state in cases:
            with pytest.raises(psycopg.Error) as err:
                d.execute("SELECT gxg_decide(%s, %s, %s, null, %s)", (ch or challenge(d), cred, n, assertion))
            assert err.value.sqlstate == state, (cred, n, assertion)
        old = challenge(d)
        owner.execute("UPDATE decider_challenges SET created_at = now() - interval '301 seconds' WHERE challenge = %s",
                      (old,))  # fmt: skip
        with pytest.raises(psycopg.Error) as err:
            d.execute("SELECT gxg_decide(%s, '\\x01', 6, null, %s)", (old, full))
        assert err.value.sqlstate == "GX403"
        assert count(owner) == 0
        good = challenge(d)
        d.execute("SELECT gxg_decide(%s, '\\x01', 6, null, %s)", (good, full))
        with pytest.raises(psycopg.Error) as err:
            d.execute("SELECT gxg_decide(%s, '\\x01', 7, null, %s)", (good, full))
        assert err.value.sqlstate == "GX409" and err.value.diag.message_primary == "assertion already used"
        for args in (("agent:t-alpha:bot", i["intent_id"], 1, i["args_hash"]),  # a machine principal
                     (HUMAN1, i["intent_id"], 2, i["args_hash"])):  # decided: approved at version 2  # fmt: skip
            with pytest.raises(psycopg.Error) as err:
                d.execute("SELECT gxg_decision_challenge(%s, %s, 'approve', %s, %s)", args)
            assert err.value.sqlstate == ("GX403" if args[0].startswith("agent") else "GX409"), args
    assert count(owner) == 1
    assert owner.execute("SELECT sign_count FROM webauthn_credentials WHERE credential_id = '\\x01'").fetchone() == (6,)


def test_enroll_function_refuses_on_its_own(pg: Any, owner: Any) -> None:
    token = decider.invite_hash("t" * 24)
    with pg.connect("gxg_admin") as admin:
        admin.execute("SELECT gxg_passkey_invite(%s, %s, %s, 600)", (HUMAN1, token, OPERATOR))
        for bad in ((HUMAN1, token, OPERATOR, 59), ("agent:x", token, OPERATOR, 600), (HUMAN1, "x", OPERATOR, 600)):
            with pytest.raises(psycopg.Error) as err:
                admin.execute("SELECT gxg_passkey_invite(%s, %s, %s, %s)", bad)
            assert err.value.sqlstate == "GX400", bad
    with pg.connect("gxg_decider") as d:
        ch = bytes(d.execute("SELECT gxg_enroll_challenge(%s, %s)", (HUMAN1, token)).fetchone()[0])
        with pytest.raises(psycopg.Error) as err:
            d.execute("SELECT gxg_enroll(%s, '\\x09', '\\x00', 0)", (b"\x00" * 32,))
        assert err.value.sqlstate == "GX403"
        owner.execute("UPDATE decider_challenges SET created_at = now() - interval '301 seconds'")
        with pytest.raises(psycopg.Error) as err:
            d.execute("SELECT gxg_enroll(%s, '\\x09', '\\x00', 0)", (ch,))
        assert err.value.sqlstate == "GX403"
        ch = bytes(d.execute("SELECT gxg_enroll_challenge(%s, %s)", (HUMAN1, token)).fetchone()[0])
        spare = bytes(d.execute("SELECT gxg_enroll_challenge(%s, %s)", (HUMAN1, token)).fetchone()[0])
        owner.execute("UPDATE passkey_invites SET expires_at = now()")
        with pytest.raises(psycopg.Error) as err:
            d.execute("SELECT gxg_enroll(%s, '\\x09', '\\x00', 0)", (ch,))
        assert err.value.sqlstate == "GX403"
        owner.execute("UPDATE passkey_invites SET expires_at = now() + interval '1 hour'")
        assert d.execute("SELECT gxg_enroll(%s, '\\x09', '\\x00', 0)", (ch,)).fetchone() == (HUMAN1,)
        with pytest.raises(psycopg.Error) as err:
            d.execute("SELECT gxg_enroll(%s, '\\x0a', '\\x00', 0)", (ch,))
        assert err.value.sqlstate == "GX409"  # the challenge is used
        with pytest.raises(psycopg.Error) as err:  # a second challenge of the same invite: the invite is used
            d.execute("SELECT gxg_enroll(%s, '\\x0b', '\\x00', 0)", (spare,))
        assert err.value.sqlstate == "GX403"
        with pytest.raises(psycopg.Error) as err:
            d.execute("SELECT gxg_enroll_challenge(%s, %s)", (HUMAN1, token))
        assert err.value.sqlstate == "GX403"  # the invite is used
    row = owner.execute("SELECT human_principal, enrolled_by FROM webauthn_credentials").fetchone()
    assert row == (HUMAN1, OPERATOR)


def test_database_refusal_text_and_no_doc_pages(api: TestClient, propose: Callable[..., Any], owner: Any) -> None:
    i = intent(propose, owner)
    r = options(api, {**i, "version": i["version"] + 1})
    assert (r.status_code, r.json()) == (409, {"message": "stale decision: version, args hash or status"})
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert api.get(path).status_code == 404
