"""Postgres wiring: connection pool, LangGraph checkpointer/store, audit log, migrations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from copilot.approval.audit import PostgresAuditStore
from copilot.config import PROJECT_ROOT, Settings
from copilot.threads import PostgresThreadRepository

SCHEMA_SQL = PROJECT_ROOT / "infra" / "postgres" / "schema.sql"

# LangGraph's Postgres checkpointer/store require autocommit and dict rows.
CONNECTION_KWARGS = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}


@dataclass
class Persistence:
    pool: AsyncConnectionPool
    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore
    audit: PostgresAuditStore
    threads: PostgresThreadRepository


async def open_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(url, min_size=min_size, max_size=max_size, open=False, kwargs=CONNECTION_KWARGS)
    await pool.open(wait=True, timeout=15)
    return pool


@asynccontextmanager
async def persistence(settings: Settings, url: str | None = None) -> AsyncIterator[Persistence]:
    """Open the pool and build every Postgres-backed component on it."""
    pool = await open_pool(url or settings.database_url)
    try:
        yield Persistence(
            pool=pool,
            checkpointer=AsyncPostgresSaver(pool),  # type: ignore[arg-type]
            store=AsyncPostgresStore(pool),  # type: ignore[arg-type]
            audit=PostgresAuditStore(pool),
            threads=PostgresThreadRepository(pool),
        )
    finally:
        await pool.close()


async def migrate(admin_url: str, app_url: str) -> None:
    """Create the app role, LangGraph tables and the application schema. Idempotent."""
    app = conninfo_to_dict(app_url)
    app_user, app_password = app.get("user"), app.get("password")
    # No prepared statements here: the schema file is a multi-statement script.
    migrate_kwargs = {**CONNECTION_KWARGS, "prepare_threshold": None}
    async with await psycopg.AsyncConnection.connect(admin_url, **migrate_kwargs) as conn:
        cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_user,))
        if await cur.fetchone() is None:
            await conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(app_user), sql.Literal(app_password))
            )
        await AsyncPostgresSaver(conn).setup()  # type: ignore[arg-type]
        await AsyncPostgresStore(conn).setup()  # type: ignore[arg-type]
        schema = SCHEMA_SQL.read_text(encoding="utf-8").replace("copilot_app", app_user or "copilot_app")
        await conn.execute(schema)  # type: ignore[arg-type]


async def ensure_database(admin_url: str, dbname: str) -> None:
    """Create a database if it does not exist (used for the test database)."""
    params: dict[str, Any] = conninfo_to_dict(admin_url)
    params["dbname"] = "postgres"
    async with await psycopg.AsyncConnection.connect(**params, autocommit=True) as conn:
        cur = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if await cur.fetchone() is None:
            await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
