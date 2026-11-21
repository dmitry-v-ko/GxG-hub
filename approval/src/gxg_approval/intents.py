"""Intent operations of the `api` process: T1 (propose), status, T6 (cancel), T9 (dispatch).

Every check of T1 that does not need the database runs before the transaction; idempotency, kill switch
(AS-05), quotas (AP-05) and auto-approval limits (AP-03) run under one per-tenant advisory lock, so
concurrent proposes of a tenant see each other's rows.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from prometheus_client import Counter
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gxg_connector_kit import _schema, canonical
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.grants import Bundle
from gxg_connector_kit.svc_jwt import Service

from . import policy, quotas
from .crypto import Encryptor
from .db import mapped_errors

log = logging.getLogger("gxg_approval")
QUOTA_REJECTIONS = Counter("gxg_quota_rejections_total", "propose refused by a quota (AP-07)", ["tenant", "requester"])
EFFECT_CLASSES = ("write_reversible", "external_effect", "high_risk")
STATUS_COLUMNS = (
    "intent_id, tenant_id, tool_key, business_task_id, status, version, approval_mode, required_approvals, "
    "rule_ids, created_at, expires_at, execution_deadline, canonical_args_hash"
)

# SQL of this module (module constants: mutation testing leaves the SQL text alone).
_SQL_RECEIVED = "SELECT gxg_received_approvals(%s)"
_SQL_AUDIT = (
    "INSERT INTO audit_events (at, actor, action, tenant_id, intent_id, to_status, details) "
    "VALUES (now(), %s, %s, %s, %s, %s, %s)"
)
_SQL_BLOCKED = "SELECT EXISTS (SELECT 1 FROM switches WHERE blocked AND scope = ANY(%s))"
_SQL_COUNTS = """
SELECT h.n, h.age, p.mine, p.total FROM
  (SELECT count(*) AS n, coalesce(extract(epoch FROM now() - min(created_at)), 0) AS age FROM intents
    WHERE tenant_id = %(t)s AND requester_principal = %(r)s AND created_at > now() - interval '1 hour') h,
  (SELECT count(*) FILTER (WHERE requester_principal = %(r)s) AS mine, count(*) AS total FROM intents
    WHERE tenant_id = %(t)s AND status = 'pending_approval') p"""  # each side bounded by an index (AP-05)
_SQL_AUTO_USED = """
SELECT count(*) FILTER (WHERE created_at > now() - interval '1 hour'), count(*)
  FROM intents WHERE tenant_id = %s AND created_at > now() - interval '1 day'
   AND approval_mode = 'auto' AND %s = ANY(rule_ids)"""  # index (tenant_id, created_at)
_SQL_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_SQL_BY_KEY = f"SELECT {STATUS_COLUMNS} FROM intents WHERE tenant_id = %s AND idempotency_key = %s"  # noqa: S608
_SQL_OWN = f"SELECT {STATUS_COLUMNS} FROM intents WHERE intent_id = %s AND tenant_id = %s AND requester_principal = %s"  # noqa: S608
_SQL_INSERT = f"""
INSERT INTO intents (intent_id, tenant_id, requester_principal, business_task_id, tool_key, tool_version,
    connector_id, execute_tool, connection_id, resource_id, effect_class, canonical_args_hash,
    args_ciphertext, preview_ciphertext, idempotency_key, policy_version, rule_ids, approval_mode,
    required_approvals, step_up_required, allowed_approvers, status, created_at, expires_at,
    approved_at, execution_deadline, execution_window_s)
VALUES (%(id)s, %(tenant)s, %(requester)s, %(task)s, %(tool)s, %(version)s, %(connector)s, %(execute)s,
    %(connection)s, %(resource)s, %(class)s, %(hash)s, %(args)s, %(preview)s, %(key)s, %(policy)s,
    %(rules)s, %(mode)s, %(approvals)s, %(step_up)s, %(approvers)s, %(status)s, now(),
    now() + make_interval(secs => %(ttl)s),
    CASE WHEN %(approved)s THEN now() END,
    CASE WHEN %(approved)s THEN now() + make_interval(secs => %(window)s) END, %(window)s)
RETURNING {STATUS_COLUMNS}"""  # noqa: S608 (constant column list)
_SQL_AUTO_DECISION = "SELECT gxg_record_auto_decision(%s)"
_SQL_OUTBOX = (
    "INSERT INTO outbox (topic, intent_id, payload, created_at, available_at) VALUES (%s, %s, %s, now(), now())"
)
_SQL_CANCEL = "SELECT gxg_cancel(%s, %s, %s, %s)"
_SQL_TOOL = "SELECT tool_key, tool_version, tenant_id FROM intents WHERE intent_id = %s"
_SQL_DISPATCH = "SELECT gxg_dispatch(%s, %s, %s, %s)"


@dataclass(frozen=True)
class Created:
    status: dict[str, Any]
    http_status: int  # 201 new, 200 idempotent repeat


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def status_doc(conn: psycopg.Connection[Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """intent-status.schema.json: no args, preview or tokens."""
    received = _one(conn, _SQL_RECEIVED, (row["intent_id"],))[0]
    doc: dict[str, Any] = {
        "intent_id": str(row["intent_id"]),
        "tenant_id": row["tenant_id"],
        "tool_key": row["tool_key"],
        "status": row["status"],
        "version": row["version"],
        "approval": {
            "mode": row["approval_mode"],
            "required_approvals": row["required_approvals"],
            "received_approvals": received,
            "rule_ids": list(row["rule_ids"]),
        },
        "created_at": _iso(row["created_at"]),
        "expires_at": _iso(row["expires_at"]),
        "execution_deadline": _iso(row["execution_deadline"]),
    }
    if row["business_task_id"] is not None:
        doc["business_task_id"] = row["business_task_id"]
    return doc


def _one(conn: psycopg.Connection[Any], sql: str, params: Any) -> tuple[Any, ...]:
    return tuple(conn.execute(sql, params).fetchone() or ())  # aggregates and function calls always return a row


def _audit(
    conn: psycopg.Connection[Any],
    actor: str,
    action: str,
    tenant: str,
    details: dict[str, Any],
    *,
    intent_id: uuid.UUID | None = None,
    to_status: str | None = None,
) -> None:
    conn.execute(
        _SQL_AUDIT,
        (actor, action, tenant, intent_id, to_status, Jsonb(details)),
    )


def _blocked(conn: psycopg.Connection[Any], tenant: str, tool: str) -> bool:
    row = conn.execute(
        _SQL_BLOCKED,
        (["global", f"tenant:{tenant}", f"tool:{tenant}:{tool}"],),
    ).fetchone()
    return bool(row and row[0])


def _counts(conn: psycopg.Connection[Any], tenant: str, requester: str) -> quotas.Counts:
    row = _one(
        conn,
        _SQL_COUNTS,
        {"t": tenant, "r": requester},
    )
    return quotas.Counts(int(row[0]), float(row[1]), int(row[2]), int(row[3]))


def _auto_used(conn: psycopg.Connection[Any], tenant: str, rule_id: str) -> tuple[int, int]:
    """AP-03: intents this rule auto-approved within the last hour and day (sliding windows)."""
    row = _one(
        conn,
        _SQL_AUTO_USED,
        (tenant, rule_id),
    )
    return int(row[0]), int(row[1])


def _validate(body: Any) -> dict[str, Any]:
    error = next(_schema.validator("intent-create").iter_errors(body), None)
    if error is not None:
        where = "/".join(map(str, error.absolute_path)) or "body"
        raise GxgError("invalid_input", f"intent-create: invalid at {where}")
    valid: dict[str, Any] = body  # the schema requires an object
    return valid


def propose(conn: psycopg.Connection[Any], bundle: Bundle, caller: Service, body: Any, encryptor: Encryptor) -> Created:
    """T1. `caller` is the verified connector (service JWT, AS-06)."""
    req = _validate(body)
    tenant, requester = req["requester"]["tenant_id"], req["requester"]["principal"]
    grants = bundle.tenants.get(tenant)
    if not caller.serves(tenant) or grants is None or requester not in grants.principals:  # AS-25, AS-08
        raise GxgError("permission_denied", "connector or requester is not registered for this tenant")
    tool = caller.tools.get((req["tool_key"], req["tool_version"]))  # AS-26: only from the bundle
    if (
        tool is None
        or tool["action"] != "propose"
        or tool["effect_class"] not in EFFECT_CLASSES
        or not tool.get("executes_via")  # nothing could execute it: refuse now, not at execution time
    ):
        raise GxgError("permission_denied", "tool is not a registered effect tool of this connector")
    connection = req["connection_id"]
    if connection not in grants.connections or req["resource_id"].partition(":")[2].partition("/")[0] != connection:
        raise GxgError("invalid_input", "connection_id is not a connection of the tenant or of the resource")
    args = canonical.normalize(req["canonical_args"])
    args_hash = canonical.hash(
        {
            "tool_key": req["tool_key"],
            "tool_version": req["tool_version"],
            "tenant_id": tenant,
            "connection_id": connection,
            "resource_id": req["resource_id"],
            "args": args,
        }
    )
    if args_hash != req["canonical_args_hash"]:
        raise GxgError("invalid_input", "canonical_args_hash does not match canonical_args")
    args_ct = encryptor.seal(tenant, "args", canonical.dumps(args))
    # EFF-33: the ui shows the canonical args under the preview, and reads only preview_ciphertext (AS-27)
    preview_ct = encryptor.seal(tenant, "preview", canonical.dumps({"preview": req["preview"], "canonical_args": args}))
    policy_doc = bundle.approval_policies.get(tenant)
    refusal: GxgError

    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        conn.execute(_SQL_LOCK, (f"gxg:t1:{tenant}",))
        existing = cur.execute(
            _SQL_BY_KEY,
            (tenant, req["idempotency_key"]),
        ).fetchone()
        if existing is not None:  # EFF-08
            if existing["canonical_args_hash"] != args_hash:
                raise GxgError("conflict", "idempotency_key is already used with other parameters")
            return Created(status_doc(conn, existing), http_status=200)
        if _blocked(conn, tenant, req["tool_key"]):  # AS-05: inside the transaction
            raise GxgError("effect_disabled", "effects are disabled for this tenant or tool")
        hit = quotas.exceeded(policy_doc and policy_doc.get("quotas"), _counts(conn, tenant, requester))
        if hit is not None:  # AP-05, AP-07
            quota, retry = hit
            QUOTA_REJECTIONS.labels(tenant, requester).inc()
            _audit(conn, caller.id, "quota_rejected", tenant, {"requester": requester, "quota": quota})
            refusal = GxgError("rate_limited", f"quota {quota} exceeded", retry_after_s=retry)
        else:
            decision = policy.evaluate(
                policy_doc,
                grants.groups,
                policy.Request(req["tool_key"], tool["effect_class"], req["resource_id"], requester),
                lambda rule_id: _auto_used(conn, tenant, rule_id),
            )
            for alert in decision.alerts:
                log.warning("ALERT policy tenant=%s: %s", tenant, alert)
            if decision.mode is None:  # AP-01.3: not stored
                _audit(
                    conn,
                    caller.id,
                    "propose_denied",
                    tenant,
                    {"requester": requester, "tool": req["tool_key"], "reason": decision.deny_reason},
                )
                refusal = GxgError("permission_denied", "denied by the approval policy")
            else:
                row = _insert(
                    conn, cur, req, tool=tool, caller=caller, decision=decision, bundle_version=bundle.version,
                    args_hash=args_hash, args_ct=args_ct, preview_ct=preview_ct,
                    approvers=approver_set(grants.roles, decision.approvers),
                )  # fmt: skip
                return Created(row, http_status=201)
    raise refusal  # after commit: the audit row of the refusal stays


def approver_set(roles: Mapping[str, frozenset[str]], narrowed: tuple[str, ...] | None) -> list[str]:
    """T2 «роль approver по bundle; в allowed_approvers», frozen at T1 so that gxg_decide can check it (M9)."""
    people = {human for human, r in roles.items() if "approver" in r}
    return sorted(people if narrowed is None else people & set(narrowed))


def _insert(
    conn: psycopg.Connection[Any],
    cur: psycopg.Cursor[dict[str, Any]],
    req: dict[str, Any],
    *,
    tool: Mapping[str, Any],
    caller: Service,
    decision: policy.Outcome,
    bundle_version: str,
    args_hash: str,
    args_ct: bytes,
    preview_ct: bytes,
    approvers: list[str],
) -> dict[str, Any]:
    tenant, requester = req["requester"]["tenant_id"], req["requester"]["principal"]
    approved = decision.mode != "human"
    intent_id = uuid.uuid4()
    row = (
        cur.execute(
            _SQL_INSERT,
            {
                "id": intent_id,
                "tenant": tenant,
                "requester": requester,
                "task": req.get("business_task_id"),
                "tool": req["tool_key"],
                "version": req["tool_version"],
                "connector": caller.id,
                "execute": tool["executes_via"],
                "connection": req["connection_id"],
                "resource": req["resource_id"],
                "class": tool["effect_class"],
                "hash": args_hash,
                "args": args_ct,
                "preview": preview_ct,
                "key": req["idempotency_key"],
                "policy": bundle_version,
                "rules": list(decision.rule_ids),
                "mode": decision.mode,
                "approvals": decision.required_approvals,
                "step_up": decision.step_up,
                "approvers": approvers if decision.mode == "human" else None,
                "status": "approved" if approved else "pending_approval",
                "ttl": decision.approval_ttl_s,
                "approved": approved,
                "window": decision.execution_window_s,
            },
        ).fetchone()
        or {}
    )  # INSERT … RETURNING always returns the row
    if decision.mode == "human" and not approvers:  # it can only expire
        log.warning("ALERT policy tenant=%s: no approver of the tenant may decide intent %s", tenant, intent_id)
    if decision.mode == "auto":
        conn.execute(_SQL_AUTO_DECISION, (intent_id,))  # EFF-03
    event: dict[str, Any] = {"intent_id": str(intent_id), "tenant_id": tenant, "status": row["status"], "version": 1}
    if not approved:  # AS-19: what the bot may say (the notifier cannot read intents); never fields or changes
        preview = req["preview"]
        event |= {
            "title": preview["title"],
            "target_label": preview["target"]["label"],
            "requester": requester,
            "expires_at": _iso(row["expires_at"]),
            "approvers": approvers,
        }
    conn.execute(_SQL_OUTBOX, ("execute" if approved else "notify", intent_id, Jsonb(event)))
    # AS-08: no verifiable assertion mechanism is configured yet (ADR-0001 variant C lands with M5).
    _audit(
        conn,
        caller.id,
        "propose",
        tenant,
        {
            "requester": requester,
            "identity_mode": "connector_attested",
            "rule_ids": list(decision.rule_ids),
            "mode": decision.mode,
            "args_hash": args_hash,
        },
        intent_id=intent_id,
        to_status=row["status"],
    )
    return status_doc(conn, row)


def get(conn: psycopg.Connection[Any], intent_id: str, tenant: str, principal: str) -> dict[str, Any]:
    """AS-11a: only the requester's own intent; any other is `not_found`, with the same body."""
    try:
        key = uuid.UUID(intent_id)
    except ValueError:
        raise GxgError("not_found", "intent not found") from None
    row = (
        conn.cursor(row_factory=dict_row)
        .execute(
            _SQL_OWN,
            (key, tenant, principal),
        )
        .fetchone()
    )
    if row is None:
        raise GxgError("not_found", "intent not found")
    return status_doc(conn, row)


def cancel(conn: psycopg.Connection[Any], intent_id: str, tenant: str, principal: str, version: int) -> dict[str, Any]:
    """T6 on behalf of the requester."""
    try:
        key = uuid.UUID(intent_id)
    except ValueError:
        raise GxgError("not_found", "intent not found") from None
    with conn.transaction(), mapped_errors():
        conn.execute(_SQL_CANCEL, (key, tenant, principal, version))
    return get(conn, intent_id, tenant, principal)


def dispatch_timeout_s(tool: Mapping[str, Any]) -> int:
    """AS-13, AS-26: from the bundle only; 60 s when the manifest sets none."""
    return int(tool.get("dispatch_timeout_s", 60))


def dispatch(conn: psycopg.Connection[Any], execution_id: uuid.UUID, intent_id: uuid.UUID, connector: Service) -> str:
    """T9 for a verified execution token; returns the dispatch deadline (RFC 3339)."""
    found = conn.execute(_SQL_TOOL, (intent_id,)).fetchone()
    # AS-25, AS-26 by the current bundle: the connector still serves the tenant and still has the tool.
    tool = connector.tools.get((found[0], found[1])) if found and connector.serves(found[2]) else None
    if tool is None:
        raise GxgError("not_found", "execution not found")
    timeout_s = dispatch_timeout_s(tool)
    with conn.transaction(), mapped_errors():
        row = _one(conn, _SQL_DISPATCH, (execution_id, intent_id, connector.id, timeout_s))
    return str(_iso(row[0]))
