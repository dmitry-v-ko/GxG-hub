"""fake-KB: the port `Backend` the wrapper talks to, over synthetic collections and documents (fixtures/kb.json).

Stands in for the KB of gxg_agents_v2 (KB Access Layer). Like that KB as far as the specs say, it knows
nothing about agents or grants: the wrapper narrows every call to the collections a caller may read (GR-16).
shortcut: in-process fixtures, not a client of the KB MCP endpoint; replace this module after the gap audit
(docs/audits/kb-gap-audit.md), the tools and grant checks stay.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gxg_connector_kit.errors import GxgError

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "kb.json"
Failure = Literal["rate_limited", "temporarily_unavailable"]
RETRY_AFTER_S = 5


@dataclass(frozen=True)
class Document:
    id: str
    collection: str
    title: str
    content: str
    updated_at: str


class Backend:
    def __init__(self, path: Path = FIXTURES) -> None:
        doc = json.loads(path.read_bytes())
        self.connections: dict[str, str] = {t: v["connection"] for t, v in doc["tenants"].items()}
        self.collections: dict[str, dict[str, str]] = {t: v["collections"] for t, v in doc["tenants"].items()}
        self.documents: dict[str, list[Document]] = {
            t: [Document(**d) for d in v["documents"]] for t, v in doc["tenants"].items()
        }
        self.failure: Failure | None = None  # tests: the KB is down or rate limited
        self.calls: Counter[str] = Counter()  # tests: KB calls per method (zero on refusals, SC-05)

    def _enter(self, kind: str) -> None:
        self.calls[kind] += 1
        if self.failure == "rate_limited":
            raise GxgError("rate_limited", "KB rate limit", retry_after_s=RETRY_AFTER_S)
        if self.failure is not None:
            raise GxgError(self.failure, "KB is not available")

    def list_collections(self, tenant: str) -> list[str]:
        self._enter("collections")
        return sorted(self.collections.get(tenant, {}))

    def get_document(self, tenant: str, document_id: str) -> Document | None:
        """The document with its real collection (GR-01, GR-03), or None. Only this tenant's KB (GR-02)."""
        self._enter("get")
        return next((d for d in self.documents.get(tenant, []) if d.id == document_id), None)

    def search(self, tenant: str, collections: list[str], query: str) -> list[Document]:
        """Matches in `collections` only, ordered by (collection, id). An empty list matches nothing."""
        self._enter("search")
        q, allowed = query.casefold(), set(collections)
        hits = [
            d
            for d in self.documents.get(tenant, [])
            if d.collection in allowed and q in f"{d.title}\n{d.content}".casefold()
        ]
        return sorted(hits, key=lambda d: (d.collection, d.id))
