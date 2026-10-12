"""Async connection pool and schema bootstrap."""

from __future__ import annotations

import logging
import os
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


async def init_pool(dsn: str | None = None, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    _load_env()
    dsn = dsn or os.environ.get("LOOPX_DATABASE_URL", DEFAULT_DSN)
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


async def ensure_schema(pool: asyncpg.Pool | None = None) -> None:
    pool = pool or get_pool()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    await pool.execute(sql)
    logger.info("schema ensured")
