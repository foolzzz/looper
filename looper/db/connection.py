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
_fenced = False
_FENCED_MESSAGE = "the database pool is fenced: this control plane lost its lock"

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SEEDS_PATH = Path(__file__).with_name("seeds.sql")


async def _check_connection_fence(conn: asyncpg.Connection) -> None:
    # asyncpg < 0.32 can finish connecting after Pool.terminate(). setup runs
    # before every checkout; raising here also makes asyncpg close the connection.
    if _fenced:
        raise RuntimeError(_FENCED_MESSAGE)


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """The pool for ``dsn``, the ops file's ``database.url`` (``looper.ops_config``)."""
    global _pool
    if _fenced:
        raise RuntimeError(_FENCED_MESSAGE)
    pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, setup=_check_connection_fence)
    # Loss can happen while create_pool is connecting, before _pool exists.
    if _fenced:
        pool.terminate()
        raise RuntimeError(_FENCED_MESSAGE)
    _pool = pool
    logger.info("database pool created (%s connections)", max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("database pool closed")


def fence() -> None:
    """Permanently revoke every pooled connection in this process, without awaiting anything."""
    global _fenced
    _fenced = True
    if _pool is not None:
        _pool.terminate()


def get_pool() -> asyncpg.Pool:
    if _fenced:
        raise RuntimeError(_FENCED_MESSAGE)
    if _pool is None:
        raise RuntimeError("database pool not initialised — call init_pool() first")
    return _pool


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """A pooled connection in a transaction: committed on exit, rolled back on error."""
    async with get_pool().acquire() as conn, conn.transaction():
        yield conn


# The schema.sql text last applied. Schema 3 uses this stable release marker
# for admission; schema 4 uses its version and post_bridge_baseline marker.
_RECORD_FINGERPRINT = """
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
_SCHEDULE_USER_GATES = "schedule_user_gates"
_RUNTIME_LAUNCHER = "runtime_launcher"
_COMMAND_SCHEDULES = "command_schedules"
_TASK_REPLY_ROUTE = "task_reply_route"
_SCHEDULE_MISSED_SINCE = "schedule_missed_since"
_SCHEDULE_DEFERRED_BY_DRAIN = "schedule_deferred_by_drain"

_COMMAND_SCHEDULES_DDL: tuple[str, ...] = (
    """ALTER TABLE schedules
        ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'agent' CHECK (kind IN ('agent', 'command')),
        ADD COLUMN IF NOT EXISTS command TEXT,
        ADD COLUMN IF NOT EXISTS env JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(env) = 'object'),
        ADD COLUMN IF NOT EXISTS env_file TEXT,
        ADD COLUMN IF NOT EXISTS success_deadline TEXT CHECK (success_deadline ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
        ADD COLUMN IF NOT EXISTS alert_failures_after INTEGER CHECK (alert_failures_after BETWEEN 1 AND 1000),
        ADD COLUMN IF NOT EXISTS alert_stale_after_seconds INTEGER CHECK (alert_stale_after_seconds BETWEEN 300 AND 2592000),
        ADD COLUMN IF NOT EXISTS alert_overlap_after INTEGER CHECK (alert_overlap_after BETWEEN 0 AND 10000),
        ADD COLUMN IF NOT EXISTS consecutive_overlaps INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_overlaps >= 0),
        ADD COLUMN IF NOT EXISTS deadline_alert_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS overlap_alert_at TIMESTAMPTZ""",
    """ALTER TABLE schedules ADD CONSTRAINT schedules_kind_fields_check CHECK (
        (kind = 'command') = (command IS NOT NULL)
        AND (kind = 'command' OR (env = '{}'::jsonb AND env_file IS NULL))
        AND (kind = 'agent' OR (gate_command IS NULL AND template_key IS NULL)))""",
    """ALTER TABLE schedule_runs
        ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS exit_code INTEGER,
        ADD COLUMN IF NOT EXISTS exit_signal INTEGER,
        ADD COLUMN IF NOT EXISTS log_path TEXT,
        ADD COLUMN IF NOT EXISTS log_bytes BIGINT,
        ADD COLUMN IF NOT EXISTS log_truncated BOOLEAN NOT NULL DEFAULT false,
        ADD COLUMN IF NOT EXISTS log_tail TEXT,
        ADD COLUMN IF NOT EXISTS runner_pid INTEGER,
        ADD COLUMN IF NOT EXISTS runner_started TEXT""",
    """ALTER TABLE schedule_runs DROP CONSTRAINT schedule_runs_status_check,
        ADD CONSTRAINT schedule_runs_status_check CHECK (status IN (
            'queued', 'gating', 'running', 'completed', 'failed', 'stopped', 'cancelled', 'timed_out',
            'gate_failed', 'skipped_overlap', 'skipped_missed', 'skipped_gate', 'skipped_budget',
            'succeeded', 'interrupted'))""",
    """ALTER TABLE schedule_notifications DROP CONSTRAINT schedule_notifications_kind_check,
        ADD CONSTRAINT schedule_notifications_kind_check CHECK (kind IN (
            'run', 'failures_alert', 'failures_recovered', 'stale_alert', 'stale_recovered',
            'deadline_alert', 'deadline_recovered', 'overlap_alert', 'overlap_recovered'))""",
    "CREATE INDEX IF NOT EXISTS idx_schedule_runs_finished ON schedule_runs (finished_at) WHERE finished_at IS NOT NULL",
)


async def _runtime_launcher_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _RUNTIME_LAUNCHER):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('agent_runtimes') AND attname = 'launcher' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _schedule_user_gates_pending(conn: asyncpg.Connection) -> bool:
    """An existing baseline still lacks ``schedules.user_gates``.

    The current ``schema.sql`` already creates the column, so an empty database
    records only ``post_bridge_baseline``. The marker is written only when this
    upgrade runs; once recorded, a missing column is not repaired.
    """
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_USER_GATES):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedules') AND attname = 'user_gates' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _command_schedules_pending(conn: asyncpg.Connection) -> bool:
    """An unmarked baseline lacks ``schedules.kind``; recorded upgrades never repair DDL."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _COMMAND_SCHEDULES):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedules') AND attname = 'kind' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _task_reply_route_pending(conn: asyncpg.Connection) -> bool:
    """An unmarked task baseline lacks the original reply route; recorded upgrades never repair DDL."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _TASK_REPLY_ROUTE):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('tasks') AND attname = 'origin_reply_route' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _schedule_missed_since_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_MISSED_SINCE):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedule_runs') AND attname = 'missed_since' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _schedule_deferred_by_drain_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_DEFERRED_BY_DRAIN):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedule_runs') AND attname = 'deferred_by_drain' "
        "AND attnum > 0 AND NOT attisdropped)")


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
    """Read-only admission and bridge preconditions, always under _SCHEMA_LOCK."""
    from .bridge_upgrade import BASELINE, BRIDGE_ERA_FINGERPRINTS, BRIDGE_PRECONDITIONS, BRIDGE_ROLE_PROMPTS

    older = ("unsupported Looper database; upgrade to v2.13.165 or a later 2.13 release first, "
             "start it once, then upgrade to this version")
    names = re.findall(r"(?im)^\s*CREATE TABLE IF NOT EXISTS\s+(\w+)", sql)
    # The version marker must be recognized even if schema.sql is incomplete.
    names.extend(("agents", "agent_configs", "task_agent_overrides", "project_skills", "schema_fingerprint", "llm_profiles",
                  "registry_schema_version"))
    # Do not initialize shadow tables over a visible installation in another namespace.
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM unnest($1::text[]) t(name) "
            "JOIN pg_class c ON c.oid = to_regclass(t.name) "
            "WHERE c.relnamespace <> quote_ident(current_schema())::regnamespace)", names):
        raise RegistrySchemaVersionError(older + "; Looper tables span namespaces")
    tables = {r["relname"] for r in await conn.fetch(
        "SELECT relname FROM pg_class WHERE relnamespace = quote_ident(current_schema())::regnamespace "
        "AND relname = ANY($1::text[])", names)}
    if not tables:
        return "empty"
    version = (await conn.fetchval("SELECT version FROM registry_schema_version WHERE id = TRUE")
               if "registry_schema_version" in tables else None)
    if "registry_schema_version" in tables and version is None:
        raise RegistrySchemaVersionError(older + "; registry schema version is missing; initialization refused")
    await check_registry_schema_version(conn, supported_version=REGISTRY_SCHEMA_VERSION)
    if version == REGISTRY_SCHEMA_VERSION:
        if "schema_migrations" not in tables or not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", BASELINE
        ):
            raise RegistrySchemaVersionError(f"registry schema version {version} is missing the {BASELINE} marker; "
                                             "startup refused")
        return "current"
    if version != 3:
        raise RegistrySchemaVersionError(older)
    fingerprint = (await conn.fetchval("SELECT sha256 FROM schema_fingerprint WHERE id = TRUE")
                   if "schema_fingerprint" in tables else None)
    if fingerprint not in BRIDGE_ERA_FINGERPRINTS:
        raise RegistrySchemaVersionError(older)
    # Check only the names the conversion needs, before serve makes a backup
    # and again before ensure_schema executes any DDL. No catalog definitions.
    columns = {(r["table_name"], r["column_name"]) for r in await conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
        [table for table, _ in BRIDGE_PRECONDITIONS])}
    for table, required in BRIDGE_PRECONDITIONS:
        if table not in tables:
            raise RegistrySchemaVersionError(f"bridge upgrade requires table {table}; startup refused")
        for column in required:
            if (table, column) not in columns:
                raise RegistrySchemaVersionError(f"bridge upgrade requires column {table}.{column}; startup refused")
    if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name='file_state_import')"):
        raise RegistrySchemaVersionError(older + "; bridge file-state import has not completed")
    prompts = {r["role"]: hashlib.sha256(r["default_prompt"].encode()).hexdigest()
               for r in await conn.fetch("SELECT role, default_prompt FROM role_configs WHERE role = ANY($1::text[])",
                                         list(BRIDGE_ROLE_PROMPTS))}
    if prompts != BRIDGE_ROLE_PROMPTS:
        raise RegistrySchemaVersionError(older + "; bridge role-prompt seeding has not completed")
    return "bridge"


async def ensure_schema(pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    from .bridge_upgrade import BASELINE, upgrade_from_bridge

    pool = pool or get_pool()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with _connection(pool) as conn, conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
        state = await _classify_schema(conn, sql)
        if state == "empty":
            await conn.execute(sql)
        elif state == "bridge":
            await upgrade_from_bridge(conn)
        # One additive upgrade for a baseline that predates schedules.user_gates,
        # including the bridge. An empty database already has the column from
        # schema.sql and records only post_bridge_baseline. Once this marker is
        # recorded, reopening never tries to repair a missing column.
        if await _schedule_user_gates_pending(conn):
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS user_gates "
                               "TEXT NOT NULL DEFAULT 'off' CHECK (user_gates IN ('off', 'on'))")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_USER_GATES)
        if await _runtime_launcher_pending(conn):
            await conn.execute("ALTER TABLE agent_runtimes ADD COLUMN IF NOT EXISTS launcher "
                               "TEXT NOT NULL DEFAULT 'native' CHECK (launcher IN ('native', 'ttadk'))")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _RUNTIME_LAUNCHER)
        if await _command_schedules_pending(conn):
            for ddl in _COMMAND_SCHEDULES_DDL:
                await conn.execute(ddl)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _COMMAND_SCHEDULES)
        if await _task_reply_route_pending(conn):
            await conn.execute("ALTER TABLE tasks ADD COLUMN origin_reply_route JSONB "
                               "CHECK (jsonb_typeof(origin_reply_route) = 'object')")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _TASK_REPLY_ROUTE)
        if await _schedule_missed_since_pending(conn):
            await conn.execute("ALTER TABLE schedule_runs ADD COLUMN missed_since TIMESTAMPTZ")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_MISSED_SINCE)
        if await _schedule_deferred_by_drain_pending(conn):
            await conn.execute("ALTER TABLE schedule_runs ADD COLUMN deferred_by_drain BOOLEAN NOT NULL DEFAULT false")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_DEFERRED_BY_DRAIN)
        # Current databases never replay initialization DDL. Missing builtins
        # are inserted without changing any saved choice or creation time.
        await conn.execute(SEEDS_PATH.read_text(encoding="utf-8"))
        if state != "current":
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", BASELINE)
        await conn.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, $1) "
                           "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version "
                           "WHERE registry_schema_version.version <> EXCLUDED.version", REGISTRY_SCHEMA_VERSION)
        # The stable current marker is sufficient even when its optional text
        # fingerprint was pruned. Opening a current database never repairs DDL.
        if state != "current" or await conn.fetchval("SELECT to_regclass('schema_fingerprint') IS NOT NULL"):
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
        return state == "bridge" or (state == "current" and (
            await _schedule_user_gates_pending(conn) or await _runtime_launcher_pending(conn)
            or await _command_schedules_pending(conn) or await _task_reply_route_pending(conn)
            or await _schedule_missed_since_pending(conn) or await _schedule_deferred_by_drain_pending(conn)))
