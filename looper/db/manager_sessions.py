"""The default Manager's group (``group:``) and other-sender direct-chat (``dm:``) sessions (``manager_sessions``).

The ``main`` session stays in ``agent_sessions``. A row is keyed by ``(manager_id, session_key)`` where
``session_key`` is ``kind:channel_id:chat``; it stores the Claude session and its running totals the way
``agent_sessions`` does.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from .connection import get_pool

_COLUMNS = ("manager_id, session_key, kind, channel_id, chat, session_id, session_totals, runtime_type, "
            "profile_id, provider_id, clean_start, pending_handoff, context_tokens, last_turn_cost_usd, "
            "scope_id, scope_version, audience, scope_fingerprint, retired_at, retired_reason, activity_cursor, "
            "created_at, last_active_at, context_policy_version")


def _row(record: asyncpg.Record) -> dict[str, Any]:
    row = dict(record)
    totals = row["session_totals"]
    row["session_totals"] = json.loads(totals) if isinstance(totals, str) else dict(totals or {})
    return row


async def get(
    manager_id: str, session_key: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> dict[str, Any] | None:
    """The stored session of ``session_key``; None when there is none."""
    pool = pool or get_pool()
    record = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM manager_sessions WHERE manager_id = $1 AND session_key = $2",
        manager_id, session_key)
    return None if record is None else _row(record)


async def save(row: Mapping[str, Any], *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    """Insert or replace a session. Omitted policy versions mark legacy context (0).

    ``context_tokens`` and ``last_turn_cost_usd`` are kept when ``row`` omits them.
    """
    pool = pool or get_pool()
    await pool.execute(
        """
        INSERT INTO manager_sessions (manager_id, session_key, kind, channel_id, chat, session_id, session_totals,
                                      runtime_type, profile_id, provider_id, clean_start, pending_handoff,
                                      context_tokens, last_turn_cost_usd, scope_id, scope_version, audience,
                                      scope_fingerprint, last_active_at, context_policy_version)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, now(), $19)
        ON CONFLICT (manager_id, session_key) DO UPDATE
        SET kind = EXCLUDED.kind, channel_id = EXCLUDED.channel_id, chat = EXCLUDED.chat,
            session_id = EXCLUDED.session_id, session_totals = EXCLUDED.session_totals,
            runtime_type = EXCLUDED.runtime_type, profile_id = EXCLUDED.profile_id,
            provider_id = EXCLUDED.provider_id, clean_start = EXCLUDED.clean_start,
            pending_handoff = EXCLUDED.pending_handoff,
            scope_id = EXCLUDED.scope_id, scope_version = EXCLUDED.scope_version,
            audience = EXCLUDED.audience, scope_fingerprint = EXCLUDED.scope_fingerprint,
            context_policy_version = EXCLUDED.context_policy_version,
            context_tokens = COALESCE(EXCLUDED.context_tokens, manager_sessions.context_tokens),
            last_turn_cost_usd = COALESCE(EXCLUDED.last_turn_cost_usd, manager_sessions.last_turn_cost_usd),
            last_active_at = now()
        """,
        row["manager_id"], row["session_key"], row["kind"], row["channel_id"], row["chat"], row["session_id"],
        json.dumps(dict(row["session_totals"])), row["runtime_type"], row["profile_id"], row["provider_id"],
        row["clean_start"], row["pending_handoff"], row.get("context_tokens"), row.get("last_turn_cost_usd"),
        row.get("scope_id"), row.get("scope_version"), row.get("audience"), row.get("scope_fingerprint"),
        row.get("context_policy_version", 0),
    )


async def active_for_chat(manager_id: str, channel_id: str, chat: str, *,
                          pool: asyncpg.Pool | asyncpg.Connection | None = None,
                          ) -> builtins.list[tuple[str, str | None]]:
    rows = await (pool or get_pool()).fetch(
        "SELECT session_key, scope_fingerprint FROM manager_sessions "
        "WHERE manager_id = $1 AND channel_id = $2 AND chat = $3 AND kind = 'group' AND retired_at IS NULL",
        manager_id, channel_id, chat)
    return [(row["session_key"], row["scope_fingerprint"]) for row in rows]


async def retire(manager_id: str, keys: Sequence[str], reason: str, *,
                 pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    await (pool or get_pool()).execute(
        "UPDATE manager_sessions SET retired_at = now(), retired_reason = $3 "
        "WHERE manager_id = $1 AND session_key = ANY($2::text[]) AND retired_at IS NULL",
        manager_id, builtins.list(keys), reason)


async def reopen(manager_id: str, key: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    """Keep totals while discarding all continuation state of a retired session."""
    await (pool or get_pool()).execute(
        "UPDATE manager_sessions SET session_id = NULL, pending_handoff = NULL, retired_at = NULL, "
        "retired_reason = NULL, activity_cursor = NULL, clean_start = false "
        "WHERE manager_id = $1 AND session_key = $2", manager_id, key)


async def touch(
    manager_id: str, session_key: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    """Mark the session as active now."""
    pool = pool or get_pool()
    await pool.execute("UPDATE manager_sessions SET last_active_at = now() "
                       "WHERE manager_id = $1 AND session_key = $2", manager_id, session_key)


async def list(
    manager_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> builtins.list[dict[str, Any]]:
    """The Manager's sessions, the most recently active first."""
    pool = pool or get_pool()
    records = await pool.fetch(
        f"SELECT {_COLUMNS} FROM manager_sessions WHERE manager_id = $1 ORDER BY last_active_at DESC, session_key",
        manager_id)
    return [_row(r) for r in records]


async def delete(
    manager_id: str, session_key: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    pool = pool or get_pool()
    await pool.execute("DELETE FROM manager_sessions WHERE manager_id = $1 AND session_key = $2",
                       manager_id, session_key)
