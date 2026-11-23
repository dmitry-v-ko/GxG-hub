"""`gxg-approval-ui` wiring (critic M9-1 F3): TLS rules of AS-15/AS-29 and that every variable reaches the app."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from kit_testing import BUNDLE_KID, BundleKit, format_public_key
from nacl.public import PrivateKey

from gxg_approval_ui import app as ui_app
from gxg_approval_ui import cli


@pytest.fixture
def environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    kit = BundleKit(tmp_path / "state.json")
    data, sig = kit.signed(built_at=time.time() - 60)
    (tmp_path / "b").mkdir()
    (tmp_path / "b/grants-bundle.json").write_bytes(data)
    (tmp_path / "b/grants-bundle.json.sig").write_bytes(sig)
    files = {
        "dsn": "host=/nonexistent dbname=x user=gxg_ui",
        "session": "session-key",
        "client": "client-secret",
        "preview": json.dumps({"t-alpha": base64.b64encode(bytes(PrivateKey.generate())).decode()}),
        "cert": "cert",
        "key": "key",
    }
    for name, value in files.items():
        (tmp_path / name).write_text(value)
    env = {
        "GXG_DB_DSN_FILE": str(tmp_path / "dsn"),
        "GXG_UI_SESSION_KEY_FILE": str(tmp_path / "session"),
        "GXG_OIDC_CLIENT_SECRET_FILE": str(tmp_path / "client"),
        "GXG_PREVIEW_KEYS_FILE": str(tmp_path / "preview"),
        "GXG_UI_PUBLIC_URL": "https://approve.example.com",
        "GXG_OIDC_ISSUER": "https://idp.example.com",
        "GXG_OIDC_CLIENT_ID": "gxg-ui",
        "GXG_LISTEN": "10.0.0.5:9443",
        "GXG_UI_TLS_CERT_FILE": str(tmp_path / "cert"),
        "GXG_UI_TLS_KEY_FILE": str(tmp_path / "key"),
        "GXG_BUNDLE_DIR": str(tmp_path / "b"),
        "GXG_BUNDLE_KEYS": json.dumps({BUNDLE_KID: format_public_key(kit.ci_key.public_key())}),
        "GXG_BUNDLE_STATE_FILE": str(tmp_path / "state.json"),
        "GXG_DECIDER_URL": "http://decider.internal:8093",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(app=app, **kw))
    monkeypatch.setattr(ui_app, "create_app", lambda deps: seen.setdefault("deps", deps) and "the-app")
    return seen


def test_tls_listener_with_every_setting(environ: dict[str, str], started: dict[str, Any]) -> None:
    cli.main()
    deps = started["deps"]
    assert {k: v for k, v in started.items() if k not in ("deps",)} == {
        "app": "the-app", "host": "10.0.0.5", "port": 9443,
        "ssl_certfile": environ["GXG_UI_TLS_CERT_FILE"], "ssl_keyfile": environ["GXG_UI_TLS_KEY_FILE"],
    }  # fmt: skip
    assert (deps.public_url, deps.oidc_issuer, deps.oidc_client_id) == (
        "https://approve.example.com", "https://idp.example.com", "gxg-ui",
    )  # fmt: skip
    assert (deps.session_key, deps.oidc_client_secret) == ("session-key", "client-secret")
    assert set(deps.preview_keys) == {"t-alpha"} and deps.bundles.readiness().ready
    assert str(deps.decider.base_url) == "http://decider.internal:8093"
    assert deps.pool.kwargs == {"autocommit": True} and deps.pool.min_size == 1 and not deps.pool.closed
    deps.pool.close()


def test_default_listener_needs_tls(environ: dict[str, str], started: dict[str, Any], monkeypatch: Any) -> None:
    monkeypatch.delenv("GXG_LISTEN")
    cli.main()
    assert (started["host"], started["port"]) == ("0.0.0.0", 8443)  # noqa: S104
    started["deps"].pool.close()
    monkeypatch.delenv("GXG_UI_TLS_CERT_FILE")
    with pytest.raises(
        SystemExit, match=r"^GXG_UI_TLS_CERT_FILE/GXG_UI_TLS_KEY_FILE are required on a non-loopback listener$"
    ):
        cli.main()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_without_tls(environ: dict[str, str], started: dict[str, Any], monkeypatch: Any, host: str) -> None:
    monkeypatch.delenv("GXG_UI_TLS_CERT_FILE")
    monkeypatch.setenv("GXG_LISTEN", f"{host}:8000")
    cli.main()
    assert "ssl_certfile" not in started and (started["host"], started["port"]) == (host, 8000)
    started["deps"].pool.close()


def test_tls_key_required_with_a_cert(environ: dict[str, str], started: dict[str, Any], monkeypatch: Any) -> None:
    monkeypatch.delenv("GXG_UI_TLS_KEY_FILE")
    with pytest.raises(SystemExit, match="GXG_UI_TLS_KEY_FILE"):
        cli.main()


@pytest.mark.parametrize("name", ["GXG_UI_PUBLIC_URL", "GXG_OIDC_ISSUER"])
def test_plain_http_urls_are_refused(
    environ: dict[str, str], started: dict[str, Any], monkeypatch: Any, name: str
) -> None:
    monkeypatch.setenv(name, "http://approve.example.com")
    with pytest.raises(SystemExit, match=f"{name} must be https"):
        cli.main()
    assert started == {}


REQUIRED = ["GXG_UI_PUBLIC_URL", "GXG_OIDC_ISSUER", "GXG_OIDC_CLIENT_ID", "GXG_DB_DSN_FILE", "GXG_PREVIEW_KEYS_FILE",
            "GXG_UI_SESSION_KEY_FILE", "GXG_OIDC_CLIENT_SECRET_FILE", "GXG_DECIDER_URL"]  # fmt: skip


@pytest.mark.parametrize("name", REQUIRED)
def test_every_setting_is_required(
    environ: dict[str, str], started: dict[str, Any], monkeypatch: Any, name: str
) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(SystemExit, match=name):
        cli.main()


def test_logging_is_info_with_the_shared_format(
    environ: dict[str, str], started: dict[str, Any], monkeypatch: Any
) -> None:
    import logging

    seen: dict[str, Any] = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    cli.main()
    assert seen == {"level": logging.INFO, "format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
    started["deps"].pool.close()


def test_access_log_has_no_query_string(environ: dict[str, str], started: dict[str, Any]) -> None:
    """SEC-17 (M9-final F-1): `GET /auth/callback?code=…` reaches the access log as the path only."""
    import logging

    access = logging.getLogger("uvicorn.access")
    for f in list(access.filters):  # other tests ran main() too
        access.removeFilter(f)
    cli.main()
    started["deps"].pool.close()
    assert [type(f) for f in access.filters] == [cli.NoQuery]
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', None, None)
    record.args = ("10.0.0.9:5000", "GET", "/auth/callback?code=SECRET-CODE&state=s", "1.1", 200)
    assert cli.NoQuery().filter(record) is True
    assert record.getMessage() == '10.0.0.9:5000 - "GET /auth/callback HTTP/1.1" 200'
    twice = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', None, None)
    twice.args = ("c", "GET", "/a?code=1?x", "1.1", 200)
    cli.NoQuery().filter(twice)
    assert twice.getMessage() == 'c - "GET /a HTTP/1.1" 200'
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "plain %s", ("x?y",), None)
    assert cli.NoQuery().filter(other) is True and other.getMessage() == "plain x?y"  # not an access line
