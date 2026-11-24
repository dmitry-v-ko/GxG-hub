"""AC-M9-01 end to end: Chromium (Playwright, preinstalled), the real `gxg-approval-ui` and `gxg-approval decider`
(AS-30, M10) processes, the test OIDC issuer
(oidc-provider-mock, a dev dependency used only here and in the compose test profile) and PostgreSQL.

The pytest process itself opens no TCP socket (AC-M0-01): servers and the browser are child processes.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

RLO, ZWSP = chr(0x202E), chr(0x200B)  # written as code points: formatters keep them visible
pytestmark = pytest.mark.e2e
HERE = Path(__file__).resolve().parent
NODE = shutil.which("node") or "node"


def _port() -> int:
    return random.randint(20000, 45000)  # noqa: S311 (a test port, not a secret)


def _start(cmd: list[str], env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    with log.open("wb") as out:
        return subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT)


@pytest.fixture
def stack(tmp_path: Path, pg: Any, world: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    oidc_port, ui_port, decider_port = _port(), _port(), _port()
    issuer = f"http://localhost:{oidc_port}"
    alice, bob = f"oidc:{issuer}:alice", f"oidc:{issuer}:bob"
    names = type(world).reload.__globals__
    original = names["tenant_file"]

    def with_e2e_humans(tenant: str, agents: list[str], connection: str) -> dict[str, Any]:
        doc: dict[str, Any] = original(tenant, agents, connection)
        if tenant == "t-alpha":
            doc["principals"]["humans"] += [{"id": alice, "roles": ["approver"]}, {"id": bob, "roles": ["approver"]}]
        return doc

    monkeypatch.setitem(names, "tenant_file", with_e2e_humans)
    world.reload()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "grants-bundle.json").write_bytes(world.signed[0])
    (bundle / "grants-bundle.json.sig").write_bytes(world.signed[1])
    secret_files = {
        "dsn": pg.dsn("gxg_ui"),
        "decider_dsn": pg.dsn("gxg_decider"),
        "session": secrets.token_urlsafe(32),
        "client": "e2e-client-secret",
        "preview": json.dumps({t: k["preview"] for t, k in world.payload_private.items()}),
    }
    for name, value in secret_files.items():
        (tmp_path / name).write_text(value)
    env = os.environ | {
        "GXG_DB_DSN_FILE": str(tmp_path / "dsn"),
        "GXG_UI_SESSION_KEY_FILE": str(tmp_path / "session"),
        "GXG_OIDC_CLIENT_SECRET_FILE": str(tmp_path / "client"),
        "GXG_PREVIEW_KEYS_FILE": str(tmp_path / "preview"),
        "GXG_UI_PUBLIC_URL": f"http://localhost:{ui_port}",
        "GXG_OIDC_ISSUER": issuer,
        "GXG_OIDC_CLIENT_ID": "gxg-ui-e2e",
        "GXG_LISTEN": f"127.0.0.1:{ui_port}",
        "GXG_BUNDLE_DIR": str(bundle),
        "GXG_BUNDLE_KEYS": world.bundle_keys(),
        "GXG_BUNDLE_STATE_FILE": str(tmp_path / "bundle-state.json"),
        "GXG_DECIDER_URL": f"http://127.0.0.1:{decider_port}",
    }
    decider_env = env | {"GXG_DB_DSN_FILE": str(tmp_path / "decider_dsn"), "GXG_LISTEN": f"127.0.0.1:{decider_port}"}
    bin_dir = Path(sys.executable).parent
    oidc = _start([str(bin_dir / "oidc-provider-mock"), "--port", str(oidc_port)], env, tmp_path / "oidc.log")
    ui = _start([str(bin_dir / "gxg-approval-ui")], env, tmp_path / "ui.log")
    decider = _start([str(bin_dir / "gxg-approval"), "decider"], decider_env, tmp_path / "decider.log")
    try:
        yield {"ui": f"http://localhost:{ui_port}", "issuer": issuer, "alice": alice, "bob": bob, "dir": tmp_path,
               "decider": f"http://127.0.0.1:{decider_port}"}  # fmt: skip
    finally:
        for p in (decider, ui, oidc):
            p.terminate()
            p.wait(timeout=10)


def browser(
    stack: dict[str, Any], step: str, intents: dict[str, str], sub: str = "alice", invite: str = ""
) -> dict[str, Any]:
    env = os.environ | {
        "E2E_DECIDER_URL": stack["decider"],
        "E2E_INVITE": invite,
        "E2E_UI_URL": stack["ui"],
        "E2E_ISSUER": stack["issuer"],
        "E2E_STATE_FILE": str(stack["dir"] / "state.json"),
        "E2E_INTENTS": json.dumps(intents),
        "E2E_SUB": sub,
    }
    run = subprocess.run([NODE, str(HERE / "e2e.mjs"), step], env=env, capture_output=True, timeout=180, check=False)
    logs = "".join(f"\n--- {p.name}\n{p.read_text(errors='replace')[-3000:]}" for p in stack["dir"].glob("*.log"))
    assert run.returncode == 0, run.stderr.decode()[-3000:] + logs
    result: dict[str, Any] = json.loads(run.stdout)
    return result


def operator_invite(pg: Any, human: str) -> str:
    """A one-time passkey invite as `gxg-approval passkeys invite` issues it (AS-30)."""
    from gxg_approval.decider import invite_hash

    token = secrets.token_urlsafe(24)
    with pg.connect("gxg_admin") as admin:
        admin.execute(
            "SELECT gxg_passkey_invite(%s, %s, 'oidc:https://idp.example.invalid:operator', 600)",
            (human, invite_hash(token)),
        )
    return token


def test_approver_signs_in_and_approves(stack: dict[str, Any], propose: Any, execute: Any, owner: Any, pg: Any) -> None:
    hostile = [
        "<script>window.__alerted = true</script>",
        "**md**",
        "[x](javascript:window.__alerted=true)",
        f"abc{RLO}dcba",
        f"zero{ZWSP}width",
        "x" * 20000,
    ]
    fields = [{"label": f"f{i}", "value": v, "kind": "multiline"} for i, v in enumerate(hostile)]
    target = {"label": "Board b1", "rid": "demo:demo-main/board/b1"}
    preview = {"schema_version": 1, "title": "Send a message", "target": target, "fields": fields}
    ids = {
        "plain": propose().json()["intent_id"],
        "hostile": propose(args={"text": "hostile", "n": 2}, preview=preview).json()["intent_id"],
        "wire": propose(tool="demo.wire.propose", args={"text": "wire", "n": 3}).json()["intent_id"],
    }
    out = browser(stack, "approve", ids, invite=operator_invite(pg, stack["alice"]))
    assert out["listed"] == 1
    assert out["cookies"] == [{"name": "__Host-gxg", "httpOnly": True, "secure": True, "sameSite": "Strict",
                               "path": "/", "domain": "localhost"}]  # fmt: skip
    pv = out["preview"]
    assert set(pv["tags"]) <= {"h2", "p", "span", "code", "dl", "dt", "dd", "pre", "details", "summary"}, pv["tags"]
    assert pv["values"][:5] == [hostile[0], hostile[1], hostile[2], "abc⟦U+202E⟧dcba", "zero⟦U+200B⟧width"]
    assert pv["lengths"][5] == 20000 and pv["warning"] is True and pv["scripts"] == 1 and pv["alerted"] is False
    assert (out["before"], out["after"]) == ("pending_approval", "approved")
    assert out["noPasskeyShown"] == 1 and out["registered"] == "Passkey зарегистрирован"
    assert (out["wireBefore"], out["wireDecisionStatus"], out["wireAfter"]) == ("pending_approval", 303, "approved")
    assert "(step-up)" in out["wireDecisions"]
    sql = (  # decided_by, step_up, whether the row carries the real browser's WebAuthn assertion
        "SELECT decided_by, step_up, webauthn_assertion ?& ARRAY['authenticator_data', 'signature'] "
        "FROM decisions WHERE intent_id = %s"
    )
    # AS-30a: an intent without step-up is decided by the button alone (no assertion), a step-up one carries the
    # assertion of the real browser (AS-30). Both paths run in the same browser.
    assert owner.execute(sql, (ids["plain"],)).fetchall() == [(stack["alice"], False, None)]
    assert owner.execute(sql, (ids["wire"],)).fetchall() == [(stack["alice"], True, True)]

    execute(ids["plain"])  # M8's executor is not on this branch: T7, T9, T11 through the real role functions
    assert browser(stack, "status", ids)["final"] == "succeeded"


def test_requester_is_refused_in_the_browser(stack: dict[str, Any], propose: Any, owner: Any, pg: Any) -> None:
    own = propose(requester=stack["alice"]).json()["intent_id"]
    out = browser(stack, "requester", {"own": own}, invite=operator_invite(pg, stack["alice"]))
    assert (out["ownStatus"], out["ownAfter"]) == (403, "pending_approval")
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (0,)
