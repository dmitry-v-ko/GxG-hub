"""`executor` process: outbox → reserve (T7) → execution token → connector `execute` → complete (T11-T13, T12a);
outcome_unknown → connector `reconcile` (T15-T17) with backoff.

Coordination only through the database (approval-service-v1 «Процессы»): any number of executors may run; rows are
claimed `FOR UPDATE SKIP LOCKED`, the reservation itself is the unique index of `gxg_reserve` (EFF-16). An `execute`
row is delivered in the reserving transaction, so a crash after it leaves a `reserved` execution that the sweeper
releases (T10). A `reconcile` row stays locked while the read-only reconcile call runs, so a crash re-queues it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import httpx
import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from prometheus_client import Counter
from psycopg.rows import dict_row

from gxg_connector_kit import _jwt, _schema, exec_token, svc_jwt
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.svc_jwt import Service

from .crypto import Decryptor
from .db import mapped_errors
from .intents import dispatch_timeout_s

log = logging.getLogger("gxg_approval.executor")
EXECUTIONS = Counter("gxg_executions_total", "completed execution attempts", ["tenant", "tool", "outcome"])
RESERVE_CONFLICTS = Counter("gxg_reserve_conflicts_total", "outbox rows whose reservation was refused")
RECONCILE_MIN_S, RECONCILE_MAX_S, RECONCILE_GIVE_UP_S = 5, 3600, 24 * 3600  # T17: backoff up to 24 h

# SQL of this module (module constants: the SQL mutation driver reaches them).
_SQL_CLAIM = """
SELECT o.id, o.topic, o.intent_id, i.status FROM outbox o JOIN intents i USING (intent_id)
 WHERE o.delivered_at IS NULL AND o.topic IN ('execute', 'reconcile')
   AND o.available_at + CASE WHEN o.attempts = 0 THEN interval '0'
                             ELSE make_interval(secs => least(5 * 2 ^ (o.attempts - 1), 300)) END <= now()
 ORDER BY o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED"""  # a kill-switched row waits 5 s, 10 s, … ≤ 5 min
_SQL_DELIVERED = "UPDATE outbox SET delivered_at = now() WHERE id = %s"
_SQL_DEFER = "UPDATE outbox SET attempts = attempts + 1 WHERE id = %s"
_SQL_RESERVE = "SELECT gxg_reserve(%s, %s)"
_SQL_INTENT = """
SELECT tenant_id, requester_principal, tool_key, tool_version, connector_id, execute_tool, connection_id,
       resource_id, canonical_args_hash, args_ciphertext, idempotency_key FROM intents WHERE intent_id = %s"""
_SQL_STATE = "SELECT state FROM executions WHERE execution_id = %s"
_SQL_COMPLETE = "SELECT gxg_complete(%s, %s, %s, %s, %s, %s)"
_SQL_UNKNOWN = """
SELECT e.execution_id, extract(epoch FROM now() - e.dispatched_at) AS waited,
       i.tenant_id, i.tool_key, i.tool_version, i.connector_id, i.execute_tool, i.idempotency_key
  FROM executions e JOIN intents i USING (intent_id) WHERE e.intent_id = %s AND e.state = 'outcome_unknown'"""
_SQL_RECONCILE = "SELECT gxg_reconcile(%s, %s, %s, %s)"
_SQL_RELEASE = "SELECT gxg_release_reserved(%s)"


@dataclass(frozen=True)
class Reserved:
    execution_id: uuid.UUID
    intent_id: uuid.UUID


class SigningKeys:
    """AS-10: `[{"seed": <base64url>, "not_before": <RFC 3339>}]`; signs with the newest key already allowed."""

    def __init__(self, doc: Callable[[], Any], now: Callable[[], float] = time.time) -> None:
        self._doc = doc
        self._now = now

    def current(self) -> Ed25519PrivateKey:
        t = self._now()
        active = [k for k in self._doc() if datetime.fromisoformat(k["not_before"]).timestamp() <= t]
        if not active:
            raise GxgError("temporarily_unavailable", "no executor signing key is active yet")
        newest = max(active, key=lambda k: datetime.fromisoformat(k["not_before"]))
        return Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(newest["seed"]))


def reconcile_delay_s(waited_s: float) -> int | None:
    """T17: next reconcile after the time already waited (doubling), 5 s … 1 h; None after 24 h (manual review)."""
    if waited_s >= RECONCILE_GIVE_UP_S:
        return None
    return int(min(max(waited_s, RECONCILE_MIN_S), RECONCILE_MAX_S))


class Executor:
    """One executor instance on its own connection (autocommit; transactions are explicit)."""

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        instance: str,
        service_id: str,
        store: BundleStore,
        decryptor: Decryptor,
        keys: SigningKeys,
        svc_key: Ed25519PrivateKey,
        http: Callable[[Service], httpx.Client],
    ) -> None:
        self.conn = conn
        self.instance = instance  # executions.reserved_by
        self.service_id = service_id  # iss of service JWTs to connectors (platform registry)
        self.store = store
        self.decryptor = decryptor
        self.keys = keys
        self.svc_key = svc_key
        self.http = http

    # --- one outbox row ---------------------------------------------------------------------------
    def take(self) -> Reserved | bool:
        """Handle one due outbox row. False: nothing due; True: handled; Reserved: reserved, call `execute` next."""
        with self.conn.transaction():
            row = self.conn.execute(_SQL_CLAIM).fetchone()
            if row is None:
                return False
            outbox_id, topic, intent_id, status = row
            reserved = None
            if topic == "reconcile" and status == "outcome_unknown":
                self._reconcile(intent_id)
            elif topic == "execute" and status == "approved":  # else moved on (cancelled, expired, executing)
                try:
                    with self.conn.transaction(), mapped_errors():
                        found = cast(
                            tuple[uuid.UUID], self.conn.execute(_SQL_RESERVE, (intent_id, self.instance)).fetchone()
                        )
                    reserved = Reserved(found[0], intent_id)
                except GxgError as exc:
                    RESERVE_CONFLICTS.inc()
                    if exc.code == "effect_disabled":  # AS-05 at T7: keep the row, try again after a backoff
                        self.conn.execute(_SQL_DEFER, (outbox_id,))
                        return True
            self.conn.execute(_SQL_DELIVERED, (outbox_id,))  # done, stale, or another executor won the reserve
            return reserved or True

    def run_once(self) -> bool:
        """Handle one due row (and execute it if it reserved). False when nothing was due."""
        taken = self.take()
        if isinstance(taken, Reserved):
            self.execute(taken)
        return taken is not False

    def run(self, stop: threading.Event, *, poll_s: float = 1.0) -> None:
        while not stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # database restart or a bug on one row: rolled back, the worker keeps running
                log.exception("executor: error")
                busy = False
            if not busy:
                stop.wait(poll_s)

    # --- execute (EFF-17, AS-12, AS-13) ---------------------------------------------------------
    def execute(self, r: Reserved) -> None:
        intent = cast(
            dict[str, Any], self.conn.cursor(row_factory=dict_row).execute(_SQL_INTENT, (r.intent_id,)).fetchone()
        )
        target = self._connector(intent)
        if target is None:  # AS-25: no token for a connector no longer registered for the tenant and tool
            return self._settle(r, intent, "not_registered")
        service, tool = target
        try:
            args = json.loads(self.decryptor.open(intent["tenant_id"], bytes(intent["args_ciphertext"])))
            token = exec_token.sign(
                self.keys.current(),
                {
                    "aud": service.id,
                    "sub": str(r.intent_id),
                    "jti": str(r.execution_id),
                    "ten": intent["tenant_id"],
                    "req": intent["requester_principal"],
                    "tool": f"{intent['tool_key']}@{intent['tool_version']}",
                    "con": intent["connection_id"],
                    "res": intent["resource_id"],
                    "ahs": intent["canonical_args_hash"],
                    "idk": intent["idempotency_key"],
                },
            )  # EFF-21: the token goes to the connector only, never into a log
            body = {"intent_id": str(r.intent_id), "execution_id": str(r.execution_id), "execution_token": token}
            resp = self._post(service, f"/internal/v1/execute/{intent['execute_tool']}", {**body, "args": args}, tool)
        except (GxgError, httpx.HTTPError):  # no key or no answer: nothing usable came back
            return self._settle(r, intent, "no_response")
        result: Any = _json(resp) if resp.status_code == 200 else None
        if not _schema.validator("effect-result").is_valid(result):
            return self._settle(r, intent, f"http_{resp.status_code}")
        details = (result["operation_id"], result.get("provider_receipt_ref"), None)
        return self._complete(r, intent, result["status"], details)

    def _settle(self, r: Reserved, intent: dict[str, Any], error: str) -> None:
        """AS-12: no effect result. Before dispatch the attempt is refused (T12a); after it, outcome_unknown (T13)."""
        state = cast(tuple[str], self.conn.execute(_SQL_STATE, (r.execution_id,)).fetchone())[0]
        if state not in ("reserved", "dispatching"):  # the sweeper moved it first (T10, T14): its outcome stands
            return _event("not_completed", r, intent, state=state, error=error)
        outcome = "refused_before_dispatch" if state == "reserved" else "outcome_unknown"
        return self._complete(r, intent, outcome, (None, None, error))

    def _connector(self, intent: dict[str, Any]) -> tuple[Service, Mapping[str, Any]] | None:
        """AS-25, AS-26 by the current bundle."""
        service = self.store.registry().get(intent["connector_id"])
        if service is None or not service.serves(intent["tenant_id"]) or not service.internal_url:
            return None
        tool = service.tools.get((intent["tool_key"], intent["tool_version"]))
        return None if tool is None else (service, tool)

    def _post(self, service: Service, path: str, body: dict[str, Any], tool: Mapping[str, Any]) -> httpx.Response:
        auth = svc_jwt.sign(self.svc_key, iss=self.service_id, aud=service.id)
        return self.http(service).post(
            path, json=body, headers={"Authorization": f"Bearer {auth}"}, timeout=dispatch_timeout_s(tool)
        )  # AS-13: the tool's dispatch timeout

    def _complete(self, r: Reserved, intent: dict[str, Any], outcome: str, details: tuple[Any, Any, Any]) -> None:
        """details: (provider_operation_id, provider_receipt_ref, error_code). A refusal (the sweeper was first)
        propagates: `run` logs it, nothing else changes."""
        with self.conn.transaction(), mapped_errors():
            self.conn.execute(_SQL_COMPLETE, (r.execution_id, self.instance, outcome, *details))
        EXECUTIONS.labels(intent["tenant_id"], intent["tool_key"], outcome).inc()
        _event("executed", r, intent, outcome=outcome, error=details[2])

    # --- reconcile (EFF-25, T15-T17), inside the claiming transaction ------------------------------
    def _reconcile(self, intent_id: uuid.UUID) -> None:
        """A refusal (resolved by a human meanwhile, T18) propagates: the row is rolled back and next time stale."""
        row = cast(
            dict[str, Any], self.conn.cursor(row_factory=dict_row).execute(_SQL_UNKNOWN, (intent_id,)).fetchone()
        )
        body = {
            "intent_id": str(intent_id),
            "execution_id": str(row["execution_id"]),
            "idempotency_key": row["idempotency_key"],
        }
        finding, doc = "unknown", cast(Any, None)
        target = self._connector(row)
        if target is not None:
            try:
                resp = self._post(target[0], f"/internal/v1/reconcile/{row['execute_tool']}", body, target[1])
                doc = _json(resp) if resp.status_code == 200 else None
            except httpx.HTTPError:
                doc = None
            if _schema.validator("reconcile-result").is_valid(doc) and doc["execution_id"] == body["execution_id"]:
                finding = doc["finding"]
        retry = reconcile_delay_s(float(row["waited"]))  # used by gxg_reconcile for `unknown` only
        if finding == "unknown" and retry is None:
            log.error("ALERT executor: intent %s outcome_unknown for 24 h: manual review (T17, T18)", intent_id)
        with self.conn.transaction(), mapped_errors():
            self.conn.execute(_SQL_RECONCILE, (intent_id, self.instance, finding, retry))

    def release_reserved(self) -> int:
        """AS-10: after a compromised key is removed, every reservation is released (its token is worthless)."""
        with self.conn.transaction():
            return cast(tuple[int], self.conn.execute(_SQL_RELEASE, (f"executor:{self.instance}",)).fetchone())[0]


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _event(name: str, r: Reserved, intent: dict[str, Any], **fields: Any) -> None:
    """One JSON log line per settled attempt: ids, tenant, tool, outcome; never args, preview or tokens (SEC-18)."""
    doc = {"event": name, "intent_id": str(r.intent_id), "execution_id": str(r.execution_id),
           "tenant": intent["tenant_id"], "tool": intent["tool_key"], **fields}  # fmt: skip
    log.info(json.dumps(doc))
