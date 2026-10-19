"""Async connection pool and schema bootstrap."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """The pool for ``dsn``, the ops file's ``database.url`` (``looper.ops_config``)."""
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
    logger.info("database pool created (%s connections)", max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("database pool closed")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised — call init_pool() first")
    return _pool


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """A pooled connection in a transaction: committed on exit, rolled back on error."""
    async with get_pool().acquire() as conn, conn.transaction():
        yield conn


# The fingerprint of the schema.sql last applied: ``schema_change_pending``
# compares it with the current file, so ``looper serve`` can back the database
# up before the file's migrations (some drop columns and tables) change it.
_RECORD_FINGERPRINT = """
CREATE TABLE IF NOT EXISTS schema_fingerprint (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    sha256      TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_fingerprint (id, sha256) VALUES (TRUE, '{sha256}')
ON CONFLICT (id) DO UPDATE SET sha256 = EXCLUDED.sha256, applied_at = now()
WHERE schema_fingerprint.sha256 <> EXCLUDED.sha256;
"""


def schema_fingerprint(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


# Serializes schema runs on one database (any schema in it): the script runs as one transaction, which takes
# this lock first, so two processes starting together cannot both create a table or migrate.
_SCHEMA_LOCK = 0x6C6F6F70  # "loop"


async def ensure_schema(pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    from ..agents import layer_effort
    from . import task_concurrency

    pool = pool or get_pool()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # One transaction: the lock is held throughout, the migrations written in Python (layer_effort, which computes
    # how the layers resolved; task_concurrency, which logs what it converts) run after the file's, and the
    # fingerprint is recorded only together with the schema it names.
    async with _connection(pool) as conn, conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK});\n{sql}")
        await layer_effort.migrate(conn)
        await task_concurrency.migrate(conn)
        await conn.execute(_RECORD_FINGERPRINT.format(sha256=schema_fingerprint(sql)))
    logger.info("schema ensured")


@asynccontextmanager
async def _connection(pool: asyncpg.Pool | asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    if isinstance(pool, asyncpg.Pool):
        async with pool.acquire() as conn:
            yield conn
    else:
        yield pool


async def schema_change_pending(conn: asyncpg.Connection) -> bool:
    """Would ``ensure_schema`` change a database that already holds Looper tables?

    True when one of schema.sql's tables exists but the recorded fingerprint
    is missing or differs from the current file's. A database without Looper
    tables is new: applying the schema loses nothing.
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    tables = re.findall(r"(?im)^\s*CREATE TABLE IF NOT EXISTS\s+(\w+)", sql)
    if not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM unnest($1::text[]) AS t(name) WHERE to_regclass(t.name) IS NOT NULL)", tables
    ):
        return False
    if not await conn.fetchval("SELECT to_regclass('schema_fingerprint') IS NOT NULL"):
        return True
    return await conn.fetchval("SELECT sha256 FROM schema_fingerprint") != schema_fingerprint(sql)
