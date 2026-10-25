"""Process-local native ingress routing; only these service writes activate live changes.

A service registry activates the committed snapshot. Without one, helpers persist
choices for the next startup only. Direct SQL and sharing a live bot across servers
are not invalidation APIs. No egress caller should resolve a chat again.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from ...db import chat_bindings as storage
from ...db.channel_configs import BUILTIN_CHANNEL_IDS
from ...db.connection import get_pool
from ..message import channel_thread_id

if TYPE_CHECKING:
    from .registry import ChannelRegistry

logger = logging.getLogger(__name__)


class RouteAction(StrEnum):
    ADMIT = "admit"
    SILENT = "silent"


@dataclass(frozen=True)
class RouteDecision:
    action: RouteAction
    manager_id: str
    explicit: bool = False


ChatResolver = Callable[[str, str], RouteDecision]
ChatStop = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class RoutingSnapshot:
    channel_id: str
    bot_manager_id: str
    bound_only: bool
    bindings: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.bound_only, bool):
            raise ValueError("bound_only must be a boolean")
        channel_thread_id(self.channel_id, "1", self.bot_manager_id, self.bot_manager_id)
        bindings = dict(self.bindings)
        for chat_id, manager_id in bindings.items():
            channel_thread_id(self.channel_id, chat_id, manager_id, self.bot_manager_id)
        object.__setattr__(self, "bindings", MappingProxyType(bindings))

    @classmethod
    def from_storage(cls, row: storage.RoutingSnapshot) -> RoutingSnapshot:
        return cls(row["channel_id"], row["manager_id"], row["bound_chats_only"],
                   {item["chat_id"]: item["manager_id"] for item in row["bindings"]})

    def resolve(self, chat_id: str) -> RouteDecision:
        target = self.bindings.get(chat_id)
        return RouteDecision(RouteAction.SILENT if target is None and self.bound_only else RouteAction.ADMIT,
                             target if target is not None else self.bot_manager_id, target is not None)


async def load_snapshot(channel_id: str, *, pool: Any = None) -> RoutingSnapshot:
    async with (pool or get_pool()).acquire() as conn, conn.transaction():
        return RoutingSnapshot.from_storage(await storage.routing_snapshot(channel_id, conn=conn))


async def observe_chat(channel_id: str, chat_id: str, *, suppressed: bool, name: str | None = None) -> None:
    try:
        await storage.record_seen(channel_id, chat_id, name, suppressed=suppressed)
    except Exception:
        # Observation can neither expose suppressed input nor lose accepted input.
        logger.warning("could not observe chat metadata for bot %s", channel_id, exc_info=True)


async def _commit_publish(transaction: Any, registry: ChannelRegistry | None,
                          snapshot: RoutingSnapshot) -> None:
    """Shield only commit + reference swap; drain even repeated caller cancellation."""
    async def finish() -> None:
        await transaction.commit()
        if registry is not None:
            registry.bindings_changed(snapshot.channel_id, snapshot)

    task = asyncio.create_task(finish())
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                task.result()
                break
    if cancelled:
        raise asyncio.CancelledError


# Server-less helpers serialize with each other but do not claim live activation.
_offline_locks: dict[str, asyncio.Lock] = {}


async def _mutate(channel_id: str, change: Callable[..., Awaitable[Any]], *,
                  registry: ChannelRegistry | None, pool: Any) -> RoutingSnapshot:
    lock = registry.mutation_lock(channel_id) if registry is not None else _offline_locks.setdefault(channel_id, asyncio.Lock())
    async with lock, (pool or get_pool()).acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await change(conn)
            snapshot = RoutingSnapshot.from_storage(await storage.routing_snapshot(channel_id, conn=conn))
        except BaseException:
            await transaction.rollback()
            raise
        await _commit_publish(transaction, registry, snapshot)
        return snapshot


async def bind_chat(channel_id: str, chat_id: str, manager_id: str, *,
                    registry: ChannelRegistry | None = None, pool: Any = None) -> RoutingSnapshot:
    return await _mutate(channel_id, lambda conn: storage.put_binding(channel_id, chat_id, manager_id, conn=conn),
                         registry=registry, pool=pool)


async def unbind_chat(channel_id: str, chat_id: str, *,
                      registry: ChannelRegistry | None = None, pool: Any = None) -> RoutingSnapshot:
    return await _mutate(channel_id, lambda conn: storage.delete_binding(channel_id, chat_id, conn=conn),
                         registry=registry, pool=pool)


async def set_chat_policy(channel_id: str, bound_only: bool, *,
                           registry: ChannelRegistry | None = None, pool: Any = None) -> RoutingSnapshot:
    return await _mutate(channel_id, lambda conn: storage.set_bound_chats_only(channel_id, bound_only, conn=conn),
                         registry=registry, pool=pool)


async def remove_bot_with_routing(channel_id: str, *, refuse_if_routed: bool = False,
                                   registry: ChannelRegistry | None = None, pool: Any = None,
                                   ) -> Literal["deleted", "missing", "builtin", "has_bindings"]:
    """Delete under the mutation lock, preserving the running adapter's owner/policy.

    The optional client guard and cascade delete share the locked bot transaction.
    Correlations and old task delivery remain usable until the adapter restarts.
    """
    if channel_id in BUILTIN_CHANNEL_IDS:
        return "builtin"
    lock = registry.mutation_lock(channel_id) if registry is not None else _offline_locks.setdefault(channel_id, asyncio.Lock())
    async with lock, (pool or get_pool()).acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            row = await conn.fetchrow("SELECT manager_id, bound_chats_only FROM channel_configs "
                                      "WHERE channel_id = $1 FOR UPDATE", channel_id)
            if row is None:
                await transaction.rollback()
                return "missing"
            if refuse_if_routed and (row["bound_chats_only"] or await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM manager_chat_bindings WHERE channel_id = $1)", channel_id)):
                await transaction.rollback()
                return "has_bindings"
            snapshot = RoutingSnapshot(channel_id, row["manager_id"], row["bound_chats_only"], {})
            await conn.execute("DELETE FROM channel_configs WHERE channel_id = $1", channel_id)
            await conn.execute("DELETE FROM channel_rejected_senders WHERE channel_id = $1", channel_id)
        except BaseException:
            await transaction.rollback()
            raise
        await _commit_publish(transaction, registry, snapshot)
    return "deleted"
