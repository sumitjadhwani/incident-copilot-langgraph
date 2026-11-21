"""Incident thread records: who started which incident, in which tenant, and its run status."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from copilot.auth.rbac import ThreadInfo

# running -> awaiting_approval -> running -> completed | failed ; interrupted after a crash
ThreadStatus = str


@dataclass
class ThreadRecord:
    thread_id: str
    tenant_id: str
    owner_id: str
    owner_username: str
    scenario_id: str | None
    title: str
    status: ThreadStatus
    created_at: str = ""
    updated_at: str = ""

    @property
    def info(self) -> ThreadInfo:
        return ThreadInfo(self.thread_id, self.tenant_id, self.owner_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ThreadRepository(Protocol):
    async def create(self, record: ThreadRecord) -> ThreadRecord: ...

    async def get(self, thread_id: str) -> ThreadRecord | None: ...

    async def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[ThreadRecord]: ...

    async def set_status(self, thread_id: str, status: ThreadStatus) -> None: ...

    async def list_by_status(self, status: ThreadStatus) -> list[ThreadRecord]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


class InMemoryThreadRepository:
    def __init__(self) -> None:
        self.rows: dict[str, ThreadRecord] = {}

    async def create(self, record: ThreadRecord) -> ThreadRecord:
        record.created_at = record.updated_at = _now()
        self.rows[record.thread_id] = record
        return record

    async def get(self, thread_id: str) -> ThreadRecord | None:
        return self.rows.get(thread_id)

    async def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[ThreadRecord]:
        rows = [r for r in self.rows.values() if r.tenant_id == tenant_id]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]

    async def set_status(self, thread_id: str, status: ThreadStatus) -> None:
        if row := self.rows.get(thread_id):
            row.status = status
            row.updated_at = _now()

    async def list_by_status(self, status: ThreadStatus) -> list[ThreadRecord]:
        return [r for r in self.rows.values() if r.status == status]


_COLS = "thread_id, tenant_id, owner_id, owner_username, scenario_id, title, status, created_at, updated_at"


def _record(row: dict[str, Any]) -> ThreadRecord:
    return ThreadRecord(
        **{**row, "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}
    )


class PostgresThreadRepository:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def create(self, record: ThreadRecord) -> ThreadRecord:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO incident_threads "
                "(thread_id, tenant_id, owner_id, owner_username, scenario_id, title, status) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
                (
                    record.thread_id,
                    record.tenant_id,
                    record.owner_id,
                    record.owner_username,
                    record.scenario_id,
                    record.title,
                    record.status,
                ),
            )
            return _record(await cur.fetchone())

    async def get(self, thread_id: str) -> ThreadRecord | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_COLS} FROM incident_threads WHERE thread_id = %s", (thread_id,))
            row = await cur.fetchone()
            return _record(row) if row else None

    async def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[ThreadRecord]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLS} FROM incident_threads WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
                (tenant_id, limit),
            )
            return [_record(r) for r in await cur.fetchall()]

    async def set_status(self, thread_id: str, status: ThreadStatus) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE incident_threads SET status = %s, updated_at = now() WHERE thread_id = %s", (status, thread_id)
            )

    async def list_by_status(self, status: ThreadStatus) -> list[ThreadRecord]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_COLS} FROM incident_threads WHERE status = %s", (status,))
            return [_record(r) for r in await cur.fetchall()]
