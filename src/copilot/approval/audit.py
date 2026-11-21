"""Append-only, hash-chained audit log of approvals and executions.

Every row stores the hash of the previous row, so editing or deleting any row breaks the chain and
`verify_chain` reports it. In Postgres the app role additionally has UPDATE/DELETE revoked and a
trigger rejects them for everyone (see infra/postgres/schema.sql).

Lifecycle for one action: `approved` (written by the approval service *before* the graph resumes)
or `rejected`, then `executed` or `failed` (written by the executor node). `blocked` records an
execution attempt without a matching approval.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from copilot.approval.hashing import canonical_json, sha256_hex

AuditStatus = Literal["approved", "rejected", "executed", "failed", "blocked"]
DECISION_STATUSES = ("approved", "rejected")
GENESIS_HASH = "0" * 64
# Arbitrary constant for pg_advisory_xact_lock: serializes chain appends.
AUDIT_LOCK_KEY = 0x0A0D17


class DuplicateAuditEntry(Exception):
    """An equivalent decision or execution is already recorded (idempotency guard)."""


@dataclass(frozen=True)
class AuditEntry:
    tenant_id: str
    thread_id: str
    approver_id: str
    status: AuditStatus
    action_tool: str
    action_payload: dict[str, Any]
    action_hash: str
    checkpoint_id: str | None = None
    trace_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditRecord(AuditEntry):
    id: int = 0
    created_at: str = ""
    prev_hash: str = GENESIS_HASH
    row_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    rows_checked: int
    first_bad_id: int | None = None
    reason: str | None = None


def format_ts(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


def compute_row_hash(prev_hash: str, created_at: str, entry: AuditEntry) -> str:
    body = {
        "prev_hash": prev_hash,
        "created_at": created_at,
        "tenant_id": entry.tenant_id,
        "thread_id": entry.thread_id,
        "checkpoint_id": entry.checkpoint_id,
        "approver_id": entry.approver_id,
        "status": entry.status,
        "action_tool": entry.action_tool,
        "action_payload": entry.action_payload,
        "action_hash": entry.action_hash,
        "trace_id": entry.trace_id,
        "detail": entry.detail,
    }
    return sha256_hex(canonical_json(body))


def verify_records(records: list[AuditRecord]) -> ChainReport:
    prev = GENESIS_HASH
    for r in records:
        if r.prev_hash != prev:
            return ChainReport(False, len(records), r.id, "prev_hash does not match the previous row")
        if compute_row_hash(r.prev_hash, r.created_at, r) != r.row_hash:
            return ChainReport(False, len(records), r.id, "row contents do not match row_hash")
        prev = r.row_hash
    return ChainReport(True, len(records))


class AuditStore(Protocol):
    async def append(self, entry: AuditEntry) -> AuditRecord: ...

    async def find(self, thread_id: str, action_hash: str, statuses: tuple[str, ...]) -> AuditRecord | None: ...

    async def list(self, tenant_id: str, thread_id: str | None = None, limit: int = 200) -> list[AuditRecord]: ...

    async def verify_chain(self) -> ChainReport: ...


def _check_unique(existing: list[AuditRecord], entry: AuditEntry) -> None:
    """Mirror of the Postgres partial unique indexes."""
    same_action = [r for r in existing if r.thread_id == entry.thread_id and r.action_hash == entry.action_hash]
    if entry.status in DECISION_STATUSES and any(r.status in DECISION_STATUSES for r in same_action):
        raise DuplicateAuditEntry("a decision for this action is already recorded")
    if entry.status == "executed" and any(r.status == "executed" for r in same_action):
        raise DuplicateAuditEntry("this action has already been executed")


class InMemoryAuditStore:
    """Same semantics as the Postgres store; used by unit tests and offline runs."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        self._lock = asyncio.Lock()

    async def append(self, entry: AuditEntry) -> AuditRecord:
        async with self._lock:
            _check_unique(self.records, entry)
            prev = self.records[-1].row_hash if self.records else GENESIS_HASH
            created_at = format_ts(datetime.now(UTC))
            record = AuditRecord(
                **asdict(entry),
                id=len(self.records) + 1,
                created_at=created_at,
                prev_hash=prev,
                row_hash=compute_row_hash(prev, created_at, entry),
            )
            self.records.append(record)
            return record

    async def find(self, thread_id: str, action_hash: str, statuses: tuple[str, ...]) -> AuditRecord | None:
        for r in reversed(self.records):
            if r.thread_id == thread_id and r.action_hash == action_hash and r.status in statuses:
                return r
        return None

    async def list(self, tenant_id: str, thread_id: str | None = None, limit: int = 200) -> list[AuditRecord]:
        rows = [r for r in self.records if r.tenant_id == tenant_id and (thread_id is None or r.thread_id == thread_id)]
        return rows[-limit:]

    async def verify_chain(self) -> ChainReport:
        return verify_records(self.records)


_COLUMNS = (
    "id, created_at, tenant_id, thread_id, checkpoint_id, approver_id, status, action_tool, "
    "action_payload, action_hash, trace_id, detail, prev_hash, row_hash"
)


def _row_to_record(row: dict[str, Any]) -> AuditRecord:
    return AuditRecord(
        id=row["id"],
        created_at=format_ts(row["created_at"]),
        tenant_id=row["tenant_id"],
        thread_id=row["thread_id"],
        checkpoint_id=row["checkpoint_id"],
        approver_id=row["approver_id"],
        status=row["status"],
        action_tool=row["action_tool"],
        action_payload=row["action_payload"],
        action_hash=row["action_hash"],
        trace_id=row["trace_id"],
        detail=row["detail"] or {},
        prev_hash=row["prev_hash"],
        row_hash=row["row_hash"],
    )


class PostgresAuditStore:
    """Audit store on a psycopg AsyncConnectionPool (dict_row, autocommit)."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def append(self, entry: AuditEntry) -> AuditRecord:
        from psycopg import errors

        created = datetime.now(UTC)
        created_at = format_ts(created)
        try:
            async with self.pool.connection() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK_KEY,))
                cur = await conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1")
                last = await cur.fetchone()
                prev = last["row_hash"] if last else GENESIS_HASH
                row_hash = compute_row_hash(prev, created_at, entry)
                cur = await conn.execute(
                    f"""
                    INSERT INTO audit_log (created_at, tenant_id, thread_id, checkpoint_id, approver_id, status,
                        action_tool, action_payload, action_hash, trace_id, detail, prev_hash, row_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s)
                    RETURNING {_COLUMNS}
                    """,
                    (
                        created,
                        entry.tenant_id,
                        entry.thread_id,
                        entry.checkpoint_id,
                        entry.approver_id,
                        entry.status,
                        entry.action_tool,
                        json.dumps(entry.action_payload),
                        entry.action_hash,
                        entry.trace_id,
                        json.dumps(entry.detail),
                        prev,
                        row_hash,
                    ),
                )
                return _row_to_record(await cur.fetchone())
        except errors.UniqueViolation as exc:
            raise DuplicateAuditEntry(str(exc).splitlines()[0]) from exc

    async def find(self, thread_id: str, action_hash: str, statuses: tuple[str, ...]) -> AuditRecord | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM audit_log WHERE thread_id = %s AND action_hash = %s "
                "AND status = ANY(%s) ORDER BY id DESC LIMIT 1",
                (thread_id, action_hash, list(statuses)),
            )
            row = await cur.fetchone()
            return _row_to_record(row) if row else None

    async def list(self, tenant_id: str, thread_id: str | None = None, limit: int = 200) -> list[AuditRecord]:
        query = f"SELECT {_COLUMNS} FROM audit_log WHERE tenant_id = %s"
        params: list[Any] = [tenant_id]
        if thread_id:
            query += " AND thread_id = %s"
            params.append(thread_id)
        query += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, params)
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in reversed(rows)]

    async def verify_chain(self) -> ChainReport:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_COLUMNS} FROM audit_log ORDER BY id")
            rows = await cur.fetchall()
        return verify_records([_row_to_record(r) for r in rows])
