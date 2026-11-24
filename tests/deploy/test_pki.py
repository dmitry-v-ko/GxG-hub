"""The internal CA of the pilot (AS-07): chain, TLS handshake, idempotence, no key in the output."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from scripts.deploy import railway_deploy as rd
from tests.deploy.mock_railway import MockRailway

HOST = "telegram.railway.internal"


def issue() -> dict[str, str]:
    values = rd.Values(rd.load_spec(), {}, rd.Out())
    return values.pki()


def test_server_certificate_is_signed_by_the_ca_for_the_private_name() -> None:
    pki = issue()
    ca = x509.load_pem_x509_certificate(pki["ca_cert"].encode())
    cert = x509.load_pem_x509_certificate(pki["server_cert"].encode())
    assert cert.issuer == ca.subject
    assert isinstance(ca.public_key(), ec.EllipticCurvePublicKey)
    cert.verify_directly_issued_by(ca)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [HOST]
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert "PRIVATE KEY" not in pki["ca_cert"] + pki["server_cert"]  # the CA key is not part of the set at all
    assert set(pki) == {"ca_cert", "server_cert", "server_key"}


def handshake(pki: dict[str, str], tmp_path: Path, server_name: str) -> None:
    """A TLS handshake in memory (no socket): the server has the connector certificate, the client trusts `ca_cert`."""
    cert_file, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_file.write_text(pki["server_cert"])
    key_file.write_text(pki["server_key"])
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_file, key_file)
    client_ctx = ssl.create_default_context(cadata=pki["ca_cert"])  # what SSL_CERT_FILE gives the executor
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname=server_name)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    for _ in range(10):
        for side, out, peer_in in ((client, c_out, s_in), (server, s_out, c_in)):
            try:
                side.do_handshake()
            except ssl.SSLWantReadError:
                pass
            data = out.read()
            if data:
                peer_in.write(data)
        if client.version() and server.version():
            return
    raise AssertionError("handshake did not finish")


def test_handshake_succeeds_for_the_connector_name_and_fails_for_another(tmp_path: Path) -> None:
    pki = issue()
    handshake(pki, tmp_path, HOST)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(pki, tmp_path, "executor.railway.internal")


def test_another_ca_is_not_trusted(tmp_path: Path) -> None:
    mine, other = issue(), issue()
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake({**mine, "ca_cert": other["ca_cert"]}, tmp_path, HOST)


def test_pki_lands_on_the_right_services_once_and_never_in_the_output(capsys: pytest.CaptureFixture[str]) -> None:
    mock = MockRailway()
    assert rd.main([], transport=mock, out=rd.Out()) == 0
    first = capsys.readouterr()
    tg, ex = mock.by_name("telegram")["vars"], mock.by_name("executor")["vars"]
    assert "BEGIN PRIVATE KEY" in tg["TELEGRAM_EXECUTE_TLS_KEY"]
    assert "BEGIN CERTIFICATE" in tg["TELEGRAM_EXECUTE_TLS_CERT"] and "BEGIN CERTIFICATE" in ex["GXG_CONFIG__SSL_CERT"]
    assert not [k for k in ex if "TLS_KEY" in k]  # the executor gets the CA certificate only
    text = first.out + first.err
    body = tg["TELEGRAM_EXECUTE_TLS_KEY"].splitlines()[1]
    assert tg["TELEGRAM_EXECUTE_TLS_KEY"] not in text and body not in text
    assert "TELEGRAM_EXECUTE_TLS_KEY" in text  # names are logged
    before = dict(mock.by_name("telegram")["vars"]), dict(ex)
    mock.mutations.clear()
    assert rd.main([], transport=mock, out=rd.Out()) == 0
    assert (
        mock.mutations == []
        and (dict(mock.by_name("telegram")["vars"]), dict(mock.by_name("executor")["vars"])) == before
    )


def test_partly_set_pki_is_refused_not_reissued(capsys: pytest.CaptureFixture[str]) -> None:
    mock = MockRailway()
    assert rd.main([], transport=mock, out=rd.Out()) == 0
    del mock.by_name("executor")["vars"]["GXG_CONFIG__SSL_CERT"]  # the CA key is gone, the pair cannot be completed
    capsys.readouterr()
    assert rd.main([], transport=mock, out=rd.Out()) == 1
    assert "partly set" in capsys.readouterr().err
