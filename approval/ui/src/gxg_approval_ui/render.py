"""Preview as plain text (EFF-30…EFF-33, AS-15, EX-19).

Templates escape every value (Jinja2 autoescape) and print it as one text node, so no value becomes an element.
Here: invisible and control characters (bidi, zero width, other Cc/Cf, line/paragraph separators) are replaced by
a visible marker `⟦U+202E⟧`, and their presence is reported so the page can warn. Nothing is cut.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})  # covers U+202A-202E, U+2066-2069, U+200B-200D, U+FEFF
KEPT = frozenset("\n\t")  # multiline text keeps its lines


def visible(text: str) -> tuple[str, bool]:
    """(text with markers, whether any marker was inserted)."""
    out, found = [], False
    for ch in text:
        if ch not in KEPT and unicodedata.category(ch) in HIDDEN_CATEGORIES:
            out.append(f"⟦U+{ord(ch):04X}⟧")
            found = True
        else:
            out.append(ch)
    return "".join(out), found


def marked(value: Any) -> tuple[Any, bool]:
    """Every string inside a JSON value made visible; keys too (they are data of the connector)."""
    if isinstance(value, str):
        return visible(value)
    if isinstance(value, list):
        items = [marked(v) for v in value]
        return [v for v, _ in items], any(f for _, f in items)
    if isinstance(value, dict):
        pairs = [(marked(k), marked(v)) for k, v in value.items()]
        return {k: v for (k, _), (v, _) in pairs}, any(fk or fv for (_, fk), (_, fv) in pairs)
    return value, False


def card(preview: dict[str, Any], args: Any) -> tuple[dict[str, Any], str, bool]:
    """(preview with markers, canonical args as indented JSON with markers, whether anything was hidden)."""
    shown, hidden = marked(preview)
    args_json, args_hidden = visible(json.dumps(args, ensure_ascii=False, indent=2, sort_keys=True))
    return shown, args_json, hidden or args_hidden
