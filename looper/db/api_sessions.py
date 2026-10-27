"""Protocol metadata and neutral, append-only API session history."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import asyncpg

from .connection import _connection, get_pool


class TranscriptMismatch(ValueError):
    """A stored session cannot be resumed with the requested transcript format."""


async def create(
    session_id: str, protocol: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    """Allocate metadata once, before a new session's first request."""
    pool = pool or get_pool()
    await pool.execute("INSERT INTO api_sessions (session_id, protocol) VALUES ($1, $2)", session_id, protocol)


async def context_tokens(
    session_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> int | None:
    pool = pool or get_pool()
    return await pool.fetchval("SELECT context_tokens FROM api_sessions WHERE session_id = $1", session_id)


async def save_context_tokens(
    session_id: str, tokens: int | None, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    """Keep the last known request size; never recreate a deleted session."""
    pool = pool or get_pool()
    await pool.execute("UPDATE api_sessions SET context_tokens = coalesce($2, context_tokens) WHERE session_id = $1",
                       session_id, tokens)


async def append(
    session_id: str, turn: int, protocol: str, model: str, items: Sequence[dict[str, Any]], *,
    pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    """Commit one completed request's already serialized neutral items atomically."""
    pool = pool or get_pool()
    async with _connection(pool) as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('api_session_items:' || $1))", session_id)
        stored_protocol = await conn.fetchval("SELECT protocol FROM api_sessions WHERE session_id = $1", session_id)
        if stored_protocol is None:
            raise TranscriptMismatch("the API session no longer exists")
        if stored_protocol != protocol:
            raise TranscriptMismatch("the stored API session uses a different protocol")
        seq = await conn.fetchval(
            "SELECT coalesce(max(seq), 0) + 1 FROM api_session_items WHERE session_id = $1", session_id)
        await conn.executemany(
            "INSERT INTO api_session_items (session_id, seq, turn, model, item) VALUES ($1, $2, $3, $4, $5::jsonb)",
            [(session_id, seq + offset, turn, model, json.dumps(item)) for offset, item in enumerate(items)])


async def load(
    session_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> list[asyncpg.Record]:
    """Read resumable history and its session protocol without creating metadata."""
    pool = pool or get_pool()
    return await pool.fetch(
        "SELECT i.*, s.protocol FROM api_session_items i JOIN api_sessions s USING (session_id) "
        "WHERE i.session_id = $1 ORDER BY i.seq", session_id)


async def last_turn(
    session_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> int | None:
    pool = pool or get_pool()
    return await pool.fetchval("SELECT max(turn) FROM api_session_items WHERE session_id = $1", session_id)


async def delete(
    session_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> int:
    """Drop metadata and its items while excluding an append to the same session."""
    pool = pool or get_pool()
    async with _connection(pool) as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('api_session_items:' || $1))", session_id)
        count = await conn.fetchval("SELECT count(*) FROM api_session_items WHERE session_id = $1", session_id)
        await conn.execute("DELETE FROM api_sessions WHERE session_id = $1", session_id)
        return count
