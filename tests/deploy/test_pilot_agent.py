"""The pilot-agent jobs: the agent token leaves the job sealed and only this process can open it; the log is parsed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts.deploy import pilot_agent

JOB = Path(__file__).resolve().parents[2] / "deploy/railway/pilot_agent_job.py"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.AGENT-TOKEN-MARKER.signature"  # noqa: S105


def load_job() -> object:
    spec = importlib.util.spec_from_file_location("pilot_agent_job", JOB)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sealed_token_roundtrip_and_the_ciphertext_hides_it(monkeypatch: pytest.MonkeyPatch) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setenv("GXG_JOB_SEAL_PUBKEY", pem.decode())
    job = load_job()
    sealed = job.seal(TOKEN)  # type: ignore[attr-defined]
    assert "AGENT-TOKEN-MARKER" not in sealed and TOKEN not in sealed
    assert pilot_agent.unseal(sealed, private) == TOKEN
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match=r".*"):
        pilot_agent.unseal(sealed, other)


def test_log_parsing_takes_ids_and_the_sealed_token() -> None:
    lines = ["noise", 'IDS {"team": "t1", "gateways": {}, "servers": {}, "tools": []}', "SEALED abc.def "]
    ids, sealed = pilot_agent.parse_log(lines)
    assert ids == {"team": "t1", "gateways": {}, "servers": {}, "tools": []} and sealed == "abc.def"
    assert pilot_agent.parse_log(["only noise"]) == (None, None)


def test_the_allowed_tools_are_exactly_the_five_of_the_pilot() -> None:
    job = load_job()
    assert job.ALLOWED == {  # type: ignore[attr-defined]
        "telegram.messages.search", "telegram.thread.read", "telegram.message.propose",
        "effects.status", "effects.cancel",
    }  # fmt: skip
    assert not [t for t in job.ALLOWED if "execute" in t or "reconcile" in t]  # type: ignore[attr-defined]


def test_sql_splitter_keeps_dollar_bodies_quotes_and_comments_whole() -> None:
    spec = importlib.util.spec_from_file_location("telegram_db_init", JOB.parent / "telegram_db_init.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = """-- a; comment
CREATE ROLE a LOGIN;
CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;
CREATE FUNCTION g() RETURNS text AS $b$ SELECT 'a;b'; $b$ LANGUAGE sql;
COMMENT ON ROLE a IS 'it''s; fine';
GRANT EXECUTE ON FUNCTION f() TO a"""
    parts = module.statements(text)
    assert [p.split()[0] for p in parts] == ["CREATE", "CREATE", "CREATE", "COMMENT", "GRANT"]
    assert "END; $$ LANGUAGE" in parts[1] and "'a;b'" in parts[2] and "it''s; fine" in parts[3]
