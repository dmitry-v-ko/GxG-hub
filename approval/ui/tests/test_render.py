"""EFF-31, EFF-32 (no database): markers for every hidden character class, nothing cut, nothing else changed."""

from __future__ import annotations

import pytest

from gxg_approval_ui import config as cli
from gxg_approval_ui import render

RLO, ZWSP = chr(0x202E), chr(0x200B)  # written as code points: formatters keep them visible
HIDDEN = [0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200B, 0x200C, 0x200D,
          0xFEFF, 0x200E, 0x200F, 0x2060, 0x00AD, 0x0000, 0x001B, 0x007F, 0x0085, 0x2028, 0x2029, 0x000D]  # fmt: skip


@pytest.mark.parametrize("code", HIDDEN)
def test_hidden_character_gets_a_marker(code: int) -> None:
    assert render.visible(f"a{chr(code)}b") == (f"a⟦U+{code:04X}⟧b", True)


@pytest.mark.parametrize("text", ["plain", "line\nline\tTab", "<script>", "**md**", "Привет", "emoji 🙂", ""])
def test_visible_text_is_unchanged(text: str) -> None:
    assert render.visible(text) == (text, False)


def test_long_text_is_not_cut() -> None:
    assert render.visible("x" * 20000)[0] == "x" * 20000


def test_marked_walks_keys_and_values() -> None:
    value = {f"a{RLO}": ["ok", {"k": f"z{ZWSP}w"}], "n": 1, "b": None}
    assert render.marked(value) == ({"a⟦U+202E⟧": ["ok", {"k": "z⟦U+200B⟧w"}], "n": 1, "b": None}, True)
    assert render.marked({"x": ["y", 2]}) == ({"x": ["y", 2]}, False)
    assert render.marked({f"{RLO}": "x"})[1] is True and render.marked([f"{RLO}"])[1] is True


def test_card_flags_hidden_characters_in_args_only() -> None:
    preview = {"title": "t", "fields": []}
    assert render.card(preview, {"text": "ok"})[2] is False
    _, args_json, hidden = render.card(preview, {"text": f"a{RLO}b"})
    assert hidden is True and "a⟦U+202E⟧b" in args_json


@pytest.mark.parametrize(
    ("url", "ok"),
    [("https://approve.example.com", True), ("http://localhost:8443", True), ("http://127.0.0.1:1", True),
     ("http://approve.example.com", False), ("http://oidc:9400", False), ("ftp://x", False)],
)  # fmt: skip
def test_tls_required_outside_loopback(url: str, ok: bool) -> None:
    if ok:
        cli.require_tls(url, "X")
    else:
        with pytest.raises(SystemExit):
            cli.require_tls(url, "X")
