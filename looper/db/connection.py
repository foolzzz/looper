"""Async connection pool and schema bootstrap."""

from __future__ import annotations

import hashlib
import logging
import json
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

REGISTRY_SCHEMA_VERSION = 4


class RegistrySchemaVersionError(RuntimeError):
    """The registry cannot be opened safely by this binary."""


async def check_registry_schema_version(conn: asyncpg.Connection, *, supported_version: int) -> int | None:
    """Read-only forward guard, also usable by the old-layout patch release.

    Call under the schema lock before executing any initialization DDL. A
    missing table is an unversioned database; an empty version table is an
    incomplete initialization, never permission to initialize over it.
    """
    if not await conn.fetchval("SELECT to_regclass('registry_schema_version') IS NOT NULL"):
        return None
    version = await conn.fetchval("SELECT version FROM registry_schema_version WHERE id = TRUE")
    if version is None:
        raise RegistrySchemaVersionError("registry schema version is missing; initialization refused")
    if version > supported_version:
        raise RegistrySchemaVersionError(
            f"registry schema version {version} exceeds supported version {supported_version}; startup refused")
    return version


async def _classify_schema(conn: asyncpg.Connection, sql: str) -> str:
    """Read-only admission gate, always called under _SCHEMA_LOCK."""
    from .bridge_upgrade import BASELINE, BRIDGE_CATALOGS, BRIDGE_MIGRATIONS, structure

    reason = "unsupported or incomplete Looper database; upgrade to v2.13.165 first"
    try:
        version = await check_registry_schema_version(conn, supported_version=REGISTRY_SCHEMA_VERSION)
        names = re.findall(r"(?im)^\s*CREATE TABLE IF NOT EXISTS\s+(\w+)", sql)
        names.extend(("agents", "agent_configs", "task_agent_overrides", "schema_fingerprint", "llm_profiles"))
        # Do not initialize shadow tables over a visible installation in another namespace.
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM unnest($1::text[]) t(name) "
                "JOIN pg_class c ON c.oid = to_regclass(t.name) "
                "WHERE c.relnamespace <> quote_ident(current_schema())::regnamespace)", names):
            raise RegistrySchemaVersionError(reason + "; Looper tables span namespaces")
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_class WHERE relnamespace = "
                "quote_ident(current_schema())::regnamespace AND relkind IN ('r', 'p', 'v', 'm', 'S', 'f')) "
                "OR EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = quote_ident(current_schema())::regnamespace) "
                "OR EXISTS (SELECT 1 FROM pg_type WHERE typnamespace = quote_ident(current_schema())::regnamespace)"):
            return "empty"
        if version not in (3, 4):
            raise RegistrySchemaVersionError(reason)
        fingerprint = await conn.fetchval("SELECT sha256 FROM schema_fingerprint WHERE id = TRUE")
        if not fingerprint or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RegistrySchemaVersionError(reason + "; missing or invalid fingerprint")
        ledger = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        # Frozen catalog witnesses keep identity independent of schema.sql cleanup.
        shape = hashlib.sha256(json.dumps(await structure(conn), sort_keys=True).encode()).hexdigest()
        if (version == 3 and set(BRIDGE_MIGRATIONS).issubset(ledger)
                and shape in BRIDGE_CATALOGS.get(fingerprint, ())):
            return "bridge"
        if version == 4 and BASELINE in ledger and shape == "166a9177ddab7d7a280377bdf1b6caccced3d8dce4c6cb1374828ca4abbd13fe":
            return "current"
        raise RegistrySchemaVersionError(reason + "; bridge identity, final structure or current baseline marker missing")
    except RegistrySchemaVersionError as exc:
        if "exceeds supported" in str(exc) or "v2.13.165" in str(exc):
            raise
        raise RegistrySchemaVersionError(f"{reason}; {exc}") from exc
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError, TypeError, ValueError) as exc:
        raise RegistrySchemaVersionError(reason) from exc


def _schema_phases(sql: str) -> dict[str, str]:
    parts = re.split(r"(?m)^-- registry-phase: (\w+)\n", sql)
    phases = dict(zip(parts[1::2], parts[2::2]))
    if list(phases) != ["expand", "legacy", "convert", "contract", "builtin"]:
        raise ValueError("schema.sql must declare expand, legacy, convert, contract and builtin phases in order")
    return phases


async def ensure_schema(pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    from .bridge_upgrade import final_constraints, upgrade_from_bridge

    pool = pool or get_pool()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with _connection(pool) as conn, conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
        state = await _classify_schema(conn, sql)
        # Fill missing builtins on every start without resetting saved choices.
        for name, phase in _schema_phases(sql).items():
            if state != "current" or name == "builtin":
                await conn.execute(phase)
        if state != "current":
            await final_constraints(conn)
            if state == "bridge":
                await upgrade_from_bridge(conn)
        await conn.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, $1) "
                           "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version "
                           "WHERE registry_schema_version.version <> EXCLUDED.version", REGISTRY_SCHEMA_VERSION)
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
    """Read-only admission and backup decision, before ensure_schema rechecks under its write lock."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
        state = await _classify_schema(conn, sql)
        if state == "empty":
            return False
        return state == "bridge" or await conn.fetchval(
            "SELECT sha256 FROM schema_fingerprint WHERE id = TRUE") != schema_fingerprint(sql)
