"""Async connection pool and schema bootstrap."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

DEFAULT_DSN = "postgresql://localhost:5432/loopx"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _load_env() -> None:
    """Load .env file from project root into os.environ (if present)."""
    if not _ENV_FILE.is_file():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        os.environ.setdefault(key, value)


def configured_dsn() -> str:
    """The database LoopX uses: ``LOOPX_DATABASE_URL`` (from the environment or .env), else the default."""
    _load_env()
    return os.environ.get("LOOPX_DATABASE_URL", DEFAULT_DSN)


async def init_pool(dsn: str | None = None, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    dsn = dsn or configured_dsn()
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
# compares it with the current file, so ``loopx serve`` can back the database
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


async def ensure_schema(pool: asyncpg.Pool | None = None) -> None:
    pool = pool or get_pool()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # One simple query runs as one transaction: the fingerprint is recorded
    # only together with the schema it names.
    await pool.execute(sql + _RECORD_FINGERPRINT.format(sha256=schema_fingerprint(sql)))
    logger.info("schema ensured")


async def schema_change_pending(conn: asyncpg.Connection) -> bool:
    """Would ``ensure_schema`` change a database that already holds LoopX tables?

    True when one of schema.sql's tables exists but the recorded fingerprint
    is missing or differs from the current file's. A database without LoopX
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
