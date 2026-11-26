"""AS-10: an operator removes a compromised key by its id, and a base64url id may start with "-"."""

from __future__ import annotations

import pytest

from gxg_approval import cli


def test_an_option_value_that_starts_with_a_dash_is_a_value() -> None:
    argv = ["keys", "remove", "--kid", "-ETr6YRgdDFLj9cOfs4Q2KRvnMICQWcJE6eQWTmQwGI"]
    assert cli._values_attached(argv) == ["keys", "remove", "--kid=-ETr6YRgdDFLj9cOfs4Q2KRvnMICQWcJE6eQWTmQwGI"]
    assert cli._values_attached(["switch", "set", "--scope", "global", "--reason", "-x"]) == [
        "switch", "set", "--scope=global", "--reason=-x",
    ]  # fmt: skip
    assert cli._values_attached(["keys", "remove", "--kid"]) == ["keys", "remove", "--kid"]  # argparse reports it
    assert cli._values_attached(["keys", "rotate"]) == ["keys", "rotate"]


def test_keys_remove_accepts_a_kid_with_a_leading_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(cli, "_keys", lambda args: seen.append(args.kid))
    cli.main(["keys", "remove", "--kid", "-abc"])  # before: argparse exit 2, "expected one argument"
    assert seen == ["-abc"]
