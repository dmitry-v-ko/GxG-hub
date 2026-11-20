"""Replaces every compact JWS in committed evidence with <jws sha256=...> (raw copies stay in evidence/raw/)."""

import hashlib
import pathlib
import re
import sys

JWS = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
for name in sys.argv[1:]:
    p = pathlib.Path(name)
    p.write_text(
        JWS.sub(lambda m: f"<jws sha256={hashlib.sha256(m.group().encode()).hexdigest()[:16]}>", p.read_text())
    )
