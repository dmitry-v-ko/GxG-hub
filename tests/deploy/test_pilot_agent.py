"""The pilot-agent jobs: the agent token leaves the job sealed and only this process can open it; the log is parsed."""

from __future__ import annotations

import importlib.util
import json
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


APIFY_SIX = {  # the tools Apify serves without a token (docs.apify.com/platform/integrations/mcp)
    "search-actors", "fetch-actor-details", "search-apify-docs", "fetch-apify-docs",
    "apify-api-search", "apify-api-details",
}  # fmt: skip


def test_the_allowed_tools_are_the_pilot_tools_kb_reads_and_apify_discovery() -> None:
    job = load_job()
    assert job.ALLOWED == {  # type: ignore[attr-defined]
        "telegram.messages.search", "telegram.thread.read", "telegram.participants.collect", "telegram.message.propose",
        "effects.status", "effects.cancel",
        "kb.documents.search", "kb.documents.get", "kb.context.get",
        *APIFY_SIX,
    }  # fmt: skip
    assert not [t for t in job.ALLOWED if "execute" in t or "reconcile" in t]  # type: ignore[attr-defined]
    assert not [t for t in job.ALLOWED if t.startswith(("call-", "add-")) or t.endswith(("-write", "-create"))]  # type: ignore[attr-defined]


def test_kb_and_apify_gateways() -> None:
    job = load_job()
    gw = job.GATEWAYS  # type: ignore[attr-defined]
    assert gw["kb-t-pilot"] == "http://kb.railway.internal:8080/mcp"
    url = gw["apify-t-pilot"]
    assert url.startswith("https://mcp.apify.com?tools=")
    assert set(url.split("tools=", 1)[1].split(",")) == APIFY_SIX  # no actor runs, nothing that needs a token
    assert job.urlsplit(url).hostname == "mcp.apify.com"  # type: ignore[attr-defined]


def test_pdp_rules_allow_exactly_the_allowed_tools_of_the_pilot_agent() -> None:
    rules = json.loads((JOB.parent / "contextforge/unified_pdp.rules.json").read_text("utf-8"))
    actions = {a.removeprefix("tools.invoke.") for a in next(r for r in rules if r["id"] == "t-pilot.pilot")["actions"]}
    assert {"kb-t-pilot-kb-documents-search", "kb-t-pilot-kb-documents-get", "kb-t-pilot-kb-context-get"} <= actions
    assert {f"apify-t-pilot-{t}" for t in APIFY_SIX} <= actions
    assert "telegram-t-pilot-telegram-participants-collect" in actions
    assert len(actions) == 6 + 3 + 6


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
