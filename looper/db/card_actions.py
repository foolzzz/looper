"""The buttons of the cards chats got (``card_actions``; assistant card actions RFC): one row per card.

A row is handled once: ``claim`` sets ``handled_at`` only while it is unset and the card has not expired, in one
statement, so two presses of a card (or a press and the confirmation typed in the chat) never both act.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import asyncpg

from .connection import _connection, get_pool

Db = asyncpg.Pool | asyncpg.Connection

_COLUMNS = ("token, kind, subject, digest, payload, manager_id, channel_id, chat, message_id, allowed, created_at, "
            "expires_at, handled_at, handled_by, handled_via, choice, outcome")


def _row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["payload"] = json.loads(out["payload"]) if isinstance(out["payload"], str) else out["payload"]
    out["allowed"] = list(out["allowed"] or [])
    return out


async def create(*, token: str, kind: str, subject: str, digest: str, payload: Mapping[str, Any],
                 manager_id: str | None, channel_id: str | None, chat: str | None, allowed: Sequence[str],
                 ttl: timedelta, pool: Db | None = None) -> None:
    pool = pool or get_pool()
    await pool.execute(
        "INSERT INTO card_actions (token, kind, subject, digest, payload, manager_id, channel_id, chat, allowed, "
        "expires_at) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, now() + $10::interval)",
        token, kind, subject, digest, json.dumps(payload, ensure_ascii=False, default=str), manager_id, channel_id,
        chat, list(allowed), ttl)


async def get(token: str, *, pool: Db | None = None) -> dict[str, Any] | None:
    """The row with its ``expired`` state as the database's clock tells it."""
    pool = pool or get_pool()
    return _row(await pool.fetchrow(f"SELECT {_COLUMNS}, expires_at <= now() AS expired FROM card_actions "
                                    "WHERE token = $1", token))


async def claim(token: str, *, by: str | None, via: str, choice: str | None, outcome: str,
                pool: Db | None = None) -> bool:
    """Handle the card ``token`` if nothing did yet and it has not expired; whether this call did. The row is locked
    first and its expiry read by the clock after the lock: a claim that waited for another one's lock is not let
    through by the time it started at."""
    pool = pool or get_pool()
    async with _connection(pool) as conn, conn.transaction():
        # Its own statement: a locking SELECT computes its columns before it waits for the lock.
        if await conn.fetchval("SELECT true FROM card_actions WHERE token = $1 FOR UPDATE", token) is None:
            return False
        return await conn.fetchval(
            "UPDATE card_actions SET handled_at = clock_timestamp(), handled_by = $2, handled_via = $3, choice = $4, "
            "outcome = $5 WHERE token = $1 AND handled_at IS NULL AND expires_at > clock_timestamp() RETURNING true",
            token, by, via, choice, outcome) is True


async def resolve_subject(kind: str, subject: str, *, by: str | None, via: str, choice: str | None, outcome: str,
                          pool: Db | None = None) -> None:
    """Settle every outstanding card for a business record in its transaction, including cards already expired.
    The subject's own row lock serializes typed commands and button presses before this write."""
    await (pool or get_pool()).execute(
        "UPDATE card_actions SET handled_at = clock_timestamp(), handled_by = $3, handled_via = $4, choice = $5, "
        "outcome = $6 WHERE kind = $1 AND subject = $2 AND handled_at IS NULL", kind, subject, by, via, choice, outcome)


async def bind(token: str, message_id: str, chat: str | None = None, *, pool: Db | None = None) -> None:
    """The channel message the card ``token`` was sent as and, when the channel says, the chat it is in; set once
    (a later send of the same card keeps them)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE card_actions SET message_id = $2, chat = COALESCE($3, chat) "
                       "WHERE token = $1 AND message_id IS NULL", token, message_id, chat)


async def finish(token: str, outcome: str, *, pool: Db | None = None) -> None:
    """What the action a claim started came to (``ok``, ``error``)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE card_actions SET outcome = $2 WHERE token = $1 AND outcome = 'running'", token, outcome)


async def lapse(tokens: Sequence[str], *, pool: Db | None = None) -> None:
    """The cards ``tokens`` no longer act: what they were for lapsed (a confirmation not given in its turn)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE card_actions SET handled_at = now(), handled_via = 'lapsed', outcome = 'lapsed' "
                       "WHERE token = ANY($1::text[]) AND handled_at IS NULL", list(tokens))


async def open_confirmations(manager_id: str | None = None, *, pool: Db | None = None) -> list[dict[str, Any]]:
    """The Manager's confirmations still waiting, oldest first, each with its age in seconds."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT {_COLUMNS}, extract(epoch FROM now() - created_at)::float8 AS age, "
        "extract(epoch FROM expires_at - now())::float8 AS remaining FROM card_actions "
        "WHERE kind = 'confirmation' AND ($1::text IS NULL OR manager_id = $1) "
        "AND handled_at IS NULL AND expires_at > now() "
        "ORDER BY created_at, token", manager_id)
    return [_row(row) for row in rows]  # type: ignore[misc]
