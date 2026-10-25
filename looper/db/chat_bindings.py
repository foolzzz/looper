"""Durable bot/chat routing choices and metadata, without enabling message routing.

Routing mutations belong to a caller transaction: lock the target Manager first
when binding, then the bot. Observation has its own per-bot advisory lock and
never changes routing, thread ownership or history. Only call ``record_seen``
for eligible messages already admitted by the sender allowlist.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypedDict

import asyncpg

from .connection import get_pool

logger = logging.getLogger(__name__)

_CHANNEL_ID = re.compile(r"channel-(telegram|lark|lark_bot)(?:-[1-9][0-9]*)?")
_MANAGER_ID = re.compile(r"manager(?:-[a-z][a-z0-9-]{1,30})?")
_CHAT_IDS = {
    "telegram": re.compile(r"-?[1-9][0-9]{0,19}"),
    "lark": re.compile(r"oc_[A-Za-z0-9]{1,125}"),
    "lark_bot": re.compile(r"oc_[A-Za-z0-9]{1,125}"),
}
_OBSERVATION_LOCK = 0x63686174  # Two-key namespace, separate from routing/schema locks.
UNBOUND_KEPT = 20
_Reader = asyncpg.Pool | asyncpg.Connection


class ChatBinding(TypedDict):
    channel_id: str
    chat_id: str
    manager_id: str
    created_at: datetime


class RoutingSnapshot(TypedDict):
    channel_id: str
    manager_id: str
    bound_chats_only: bool
    bindings: list[ChatBinding]


class ChannelChat(TypedDict):
    channel_id: str
    chat_id: str
    name: str | None
    count: int | None
    last_at: datetime | None
    manager_id: str | None
    binding_created_at: datetime | None
    unbound_count: int | None
    unbound_last_at: datetime | None


def _kind(channel_id: str) -> str:
    match = _CHANNEL_ID.fullmatch(channel_id) if isinstance(channel_id, str) else None
    if match is None:
        raise ValueError("channel_id must name a native Telegram or Lark bot")
    return match[1]


def _chat_id(channel_id: str, chat_id: str) -> None:
    if not isinstance(chat_id, str) or not _CHAT_IDS[_kind(channel_id)].fullmatch(chat_id):
        raise ValueError("chat_id must be a native chat id for this bot type")


def _manager_id(manager_id: str) -> None:
    if not isinstance(manager_id, str) or not _MANAGER_ID.fullmatch(manager_id):
        raise ValueError("manager_id must name a Manager")


def _transaction(conn: asyncpg.Connection) -> None:
    if not conn.is_in_transaction():
        raise ValueError("a caller transaction is required")


async def _channel(channel_id: str, reader: _Reader, *, lock: bool = False) -> asyncpg.Record:
    kind = _kind(channel_id)
    row = await reader.fetchrow(
        "SELECT channel_id, channel_type, manager_id, bound_chats_only FROM channel_configs "
        "WHERE channel_id = $1" + (" FOR UPDATE" if lock else ""), channel_id)
    if row is None:
        raise LookupError("channel not found")
    if row["channel_type"] != kind:
        raise ValueError("channel must be a native Telegram or Lark bot")
    return row


async def list_bindings(channel_id: str | None = None, manager_id: str | None = None, *,
                        pool: _Reader | None = None) -> list[ChatBinding]:
    """Explicit rows only, ordered by bot then native chat id; datetimes stay raw."""
    reader = pool or get_pool()
    if channel_id is not None:
        await _channel(channel_id, reader)
    if manager_id is not None:
        _manager_id(manager_id)
        if not await reader.fetchval("SELECT deleted_at IS NULL FROM managers WHERE id = $1", manager_id):
            raise LookupError("Manager not found")
    rows = await reader.fetch(
        "SELECT channel_id, chat_id, manager_id, created_at FROM manager_chat_bindings "
        "WHERE ($1::text IS NULL OR channel_id = $1) AND ($2::text IS NULL OR manager_id = $2) "
        "ORDER BY channel_id, chat_id", channel_id, manager_id)
    return [ChatBinding(**dict(row)) for row in rows]


async def put_binding(channel_id: str, chat_id: str, manager_id: str, *,
                      conn: asyncpg.Connection, before_write: Callable[[], Awaitable[None]] | None = None) -> ChatBinding:
    """Bind even to the bot's own Manager; rebinding keeps the original creation time.

    KEY SHARE conflicts with Manager deletion's FOR UPDATE. Recheck deleted_at
    under that lock before taking the channel lock, including after a waiter wakes.
    Disabled Managers are valid targets.
    """
    _chat_id(channel_id, chat_id)
    _manager_id(manager_id)
    _transaction(conn)
    live = await conn.fetchval("SELECT deleted_at IS NULL FROM managers WHERE id = $1 FOR KEY SHARE", manager_id)
    if not live:
        raise LookupError("Manager not found")
    await _channel(channel_id, conn, lock=True)
    if before_write is not None:
        await before_write()
    row = await conn.fetchrow(
        "INSERT INTO manager_chat_bindings (channel_id, chat_id, manager_id) VALUES ($1, $2, $3) "
        "ON CONFLICT (channel_id, chat_id) DO UPDATE SET manager_id = EXCLUDED.manager_id "
        "RETURNING channel_id, chat_id, manager_id, created_at", channel_id, chat_id, manager_id)
    return ChatBinding(**dict(row))


async def delete_binding(channel_id: str, chat_id: str, *, conn: asyncpg.Connection,
                         before_write: Callable[[], Awaitable[None]] | None = None) -> bool:
    """Unbind idempotently. Suppression history and all chat/task ownership stay."""
    _chat_id(channel_id, chat_id)
    _transaction(conn)
    await _channel(channel_id, conn, lock=True)
    if before_write is not None:
        await before_write()
    return await conn.fetchval(
        "DELETE FROM manager_chat_bindings WHERE channel_id = $1 AND chat_id = $2 RETURNING true",
        channel_id, chat_id) is True


async def routing_snapshot(channel_id: str, *, conn: asyncpg.Connection) -> RoutingSnapshot:
    """A consistent bot policy and explicit rows, held stable until the caller commits."""
    _transaction(conn)
    row = await _channel(channel_id, conn, lock=True)
    return RoutingSnapshot(channel_id=channel_id, manager_id=row["manager_id"],
                           bound_chats_only=row["bound_chats_only"],
                           bindings=await list_bindings(channel_id, pool=conn))


async def set_bound_chats_only(channel_id: str, enabled: bool, *, conn: asyncpg.Connection,
                              before_write: Callable[[], Awaitable[None]] | None = None) -> RoutingSnapshot:
    """Dedicated policy write; ordinary bot config saves must never set this column."""
    if not isinstance(enabled, bool):
        raise ValueError("bound_chats_only must be a boolean")
    _transaction(conn)
    await _channel(channel_id, conn, lock=True)
    if before_write is not None:
        await before_write()
    await conn.execute("UPDATE channel_configs SET bound_chats_only = $2, updated_at = now() WHERE channel_id = $1",
                       channel_id, enabled)
    return await routing_snapshot(channel_id, conn=conn)


async def record_seen(channel_id: str, chat_id: str, name: str | None = None, *, suppressed: bool,
                      pool: asyncpg.Pool | None = None) -> None:
    """Observe metadata only; ``suppressed`` means bound-only refused this message.

    Keep every observed identity until bot removal, but only the newest 20
    suppressed identities. Binding never clears a historical suppressed row.
    Missing bots and recording failures cannot affect message delivery or silence.
    """
    _chat_id(channel_id, chat_id)
    if name is not None and not isinstance(name, str):
        raise ValueError("name must be a string or null")
    if not isinstance(suppressed, bool):
        raise ValueError("suppressed must be a boolean")
    name = (name.strip()[:200] or None) if name is not None else None
    try:
        pool = pool or get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::integer, hashtext($2))", _OBSERVATION_LOCK, channel_id)
            # The FK's KEY SHARE protects against removal until both upserts commit.
            row = await conn.fetchrow(
                "SELECT channel_type FROM channel_configs WHERE channel_id = $1 FOR KEY SHARE", channel_id)
            if row is None:
                return
            if row["channel_type"] != _kind(channel_id):
                raise ValueError("channel must be a native Telegram or Lark bot")
            for table in ("channel_seen_chats", "channel_unbound_chats") if suppressed else ("channel_seen_chats",):
                await conn.execute(
                    f"INSERT INTO {table} (channel_id, chat_id, name, last_at) VALUES ($1, $2, $3, clock_timestamp()) "
                    f"ON CONFLICT (channel_id, chat_id) DO UPDATE SET count = {table}.count + 1, "
                    f"name = coalesce(EXCLUDED.name, {table}.name), last_at = EXCLUDED.last_at", channel_id, chat_id, name)
            if suppressed:
                await conn.execute(
                    "DELETE FROM channel_unbound_chats WHERE channel_id = $1 AND chat_id NOT IN ("
                    "SELECT chat_id FROM channel_unbound_chats WHERE channel_id = $1 "
                    "ORDER BY last_at DESC, chat_id ASC LIMIT $2)", channel_id, UNBOUND_KEPT)
    except ValueError:
        raise
    except Exception:
        logger.exception("could not record chat metadata for bot %s", channel_id)


async def list_channel_chats(channel_id: str, limit: int = 100, after: str | None = None, *,
                             pool: _Reader | None = None) -> list[ChannelChat]:
    """Page the full observed catalog plus explicit bindings, by chat_id ASC.

    ``count``/``last_at`` describe observations; ``unbound_*`` describe only
    suppressed messages. A never-observed binding has null observation fields.
    The cursor is an exclusive native id, ordered as text, not as an integer.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if after is not None:
        _chat_id(channel_id, after)
    reader = pool or get_pool()
    await _channel(channel_id, reader)
    rows = await reader.fetch(
        "WITH identities AS ("
        "SELECT chat_id FROM channel_seen_chats WHERE channel_id = $1 UNION "
        "SELECT chat_id FROM manager_chat_bindings WHERE channel_id = $1) "
        "SELECT $1::text AS channel_id, i.chat_id, s.name, s.count, s.last_at, b.manager_id, "
        "b.created_at AS binding_created_at, u.count AS unbound_count, u.last_at AS unbound_last_at "
        "FROM identities i LEFT JOIN channel_seen_chats s ON s.channel_id = $1 AND s.chat_id = i.chat_id "
        "LEFT JOIN manager_chat_bindings b ON b.channel_id = $1 AND b.chat_id = i.chat_id "
        "LEFT JOIN channel_unbound_chats u ON u.channel_id = $1 AND u.chat_id = i.chat_id "
        "WHERE ($2::text IS NULL OR i.chat_id > $2) ORDER BY i.chat_id ASC LIMIT $3", channel_id, after, limit)
    return [ChannelChat(**dict(row)) for row in rows]
