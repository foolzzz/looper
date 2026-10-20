"""Read-only legacy registry preflight. No schema bootstrap or runtime discovery."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from ..agents.registry_migration import LegacySnapshot, plan, snapshot
from ..agents.profile_registry import RuntimeSpec
from ..agents.runtimes import Capability
from .connection import _connection, get_pool


async def _read_snapshot(conn: asyncpg.Connection) -> LegacySnapshot:
    # Read raw rows: export's JOINs hide dangling profile references, and its
    # projection omits creation order needed to choose the canonical account.
    runtimes = [dict(r) for r in await conn.fetch("SELECT * FROM agent_runtimes ORDER BY id")]
    providers = [dict(r) for r in await conn.fetch(
        "SELECT p.*, s.api_key FROM llm_providers p LEFT JOIN llm_provider_secrets s ON s.provider_id = p.id "
        "ORDER BY p.id")]
    profiles = [dict(r) for r in await conn.fetch("SELECT * FROM agent_profiles ORDER BY id")]
    for row in providers:
        for key in ("models", "env"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    sessions = []
    for table, column in (("agent_sessions", "provider_id"), ("schedules", "planner_session_provider")):
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass($1) "
                               "AND attname = $2 AND NOT attisdropped)", table, column):
            sessions.extend(r[column] for r in await conn.fetch(f"SELECT {column} FROM {table}"))
    return snapshot(
        [RuntimeSpec(r["id"], r["name"], r["type"], r.get("executable") or "",
                     frozenset(Capability(c) for c in r.get("capabilities") or ())) for r in runtimes],
        providers, profiles, session_provider_ids=sessions)


async def read_snapshot(pool: asyncpg.Pool | asyncpg.Connection | None = None) -> LegacySnapshot:
    """Read a consistent private snapshot; existing transactions must be read-only
    and use repeatable-read or serializable isolation.

    A caller applying changes must take a new snapshot and recompute ``plan``
    in its own migration transaction, rather than reuse a dry-run result.
    """
    async with _connection(pool if pool is not None else get_pool()) as conn:
        if conn.is_in_transaction():
            if await conn.fetchval("SHOW transaction_read_only") != "on":
                raise ValueError("legacy preflight requires a read-only transaction")
            if await conn.fetchval("SHOW transaction_isolation") not in {"repeatable read", "serializable"}:
                raise ValueError("legacy preflight requires a repeatable-read snapshot")
            return await _read_snapshot(conn)
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            return await _read_snapshot(conn)


async def dry_run(pool: asyncpg.Pool | asyncpg.Connection | None = None) -> dict[str, Any]:
    """Return only the RFC 9.1 counts, never the snapshot or converted rows."""
    return plan(await read_snapshot(pool)).report
