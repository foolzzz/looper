"""Process-local native ingress routing; only these service writes activate live changes.

A service registry activates the committed snapshot. Without one, helpers persist
choices for the next startup only. Direct SQL and sharing a live bot across servers
are not invalidation APIs. No egress caller should resolve a chat again.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
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


def boundary_log(log: logging.Logger, event: str, **fields: Any) -> None:
    """Keep observability outside the decision, even if a logging handler fails."""
    try:
        log.info(event + " %s", " ".join(f"{key}={value}" for key, value in fields.items()), extra=fields)
    except Exception:
        pass


def log_route(log: logging.Logger, channel_id: str, chat_id: str, route: RouteDecision,
              *, reason: str | None = None, outcome: str = "admitted") -> None:
    silent = route.action is RouteAction.SILENT
    boundary_log(log, "chat_route", channel_id=channel_id, chat_id=chat_id,
                 manager_id=None if silent else route.manager_id, action=route.action.value,
                 reason=reason or ("only_bound_chats" if silent else "explicit_binding" if route.explicit else "bot_manager"),
                 outcome="silenced" if silent else outcome)


def log_refusal(log: logging.Logger, operation: Any, principal: Any, reason: str) -> None:
    if operation is not None and operation.operation_id in (
            "manager_chat_bindings.put", "manager_chat_bindings.delete", "channel_chats.set_policy"):
        boundary_log(log, "chat_routing_write", action=operation.operation_id,
                     principal_kind=principal.kind, outcome="refused", reason=reason)


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
                          snapshot: RoutingSnapshot, committed: Callable[[], None] | None = None) -> None:
    """Shield only commit + reference swap; drain even repeated caller cancellation."""
    async def finish() -> None:
        await transaction.commit()
        if registry is not None:
            registry.bindings_changed(snapshot.channel_id, snapshot)
        if committed is not None:
            committed()

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


async def _mutate(channel_id: str, change: Callable[..., Awaitable[Any]],
                  update: Callable[[RoutingSnapshot], RoutingSnapshot], *,
                  registry: ChannelRegistry | None, pool: Any, action: str, principal_kind: Literal["client", "operator"],
                  chat_id: str | None = None) -> tuple[RoutingSnapshot, Any]:
    lock = registry.mutation_lock(channel_id) if registry is not None else _offline_locks.setdefault(channel_id, asyncio.Lock())
    async with lock, (pool or get_pool()).acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        before: RoutingSnapshot

        async def capture() -> None:
            nonlocal before
            # Move the existing snapshot read before the write, after its normal locks.
            before = RoutingSnapshot.from_storage(await storage.routing_snapshot(channel_id, conn=conn))

        try:
            result = await change(conn, capture)
            snapshot = update(before)
        except BaseException:
            await transaction.rollback()
            raise

        def committed() -> None:
            values = ({"old_manager_id": before.bindings.get(chat_id), "new_manager_id": snapshot.bindings.get(chat_id)}
                      if chat_id is not None else
                      {"old_bound_chats_only": before.bound_only, "new_bound_chats_only": snapshot.bound_only})
            boundary_log(logger, "chat_routing_write", channel_id=channel_id, chat_id=chat_id,
                         action=action, principal_kind=principal_kind, outcome="committed", **values)

        await _commit_publish(transaction, registry, snapshot, committed)
        return snapshot, result


async def bind_chat(channel_id: str, chat_id: str, manager_id: str, *,
                    registry: ChannelRegistry | None = None, pool: Any = None, principal_kind: Literal["client", "operator"] = "operator",
                    readback: Callable[[Any], Awaitable[None]] | None = None) -> RoutingSnapshot:
    """Capture any response under the same lock as the write, before commit/publication."""
    async def change(conn: Any, capture: Any) -> None:
        await storage.put_binding(channel_id, chat_id, manager_id, conn=conn, before_write=capture)
        if readback is not None:
            await readback(conn)

    snapshot, _ = await _mutate(channel_id, change,
        lambda old: replace(old, bindings={**old.bindings, chat_id: manager_id}), registry=registry, pool=pool,
        action="manager_chat_bindings.put", principal_kind=principal_kind, chat_id=chat_id)
    return snapshot


async def unbind_chat(channel_id: str, chat_id: str, *,
                      registry: ChannelRegistry | None = None, pool: Any = None, principal_kind: Literal["client", "operator"] = "operator") -> bool:
    _, deleted = await _mutate(channel_id,
        lambda conn, capture: storage.delete_binding(channel_id, chat_id, conn=conn, before_write=capture),
        lambda old: replace(old, bindings={key: value for key, value in old.bindings.items() if key != chat_id}),
        registry=registry, pool=pool, action="manager_chat_bindings.delete", principal_kind=principal_kind, chat_id=chat_id)
    return deleted


async def set_chat_policy(channel_id: str, bound_only: bool, *,
                           registry: ChannelRegistry | None = None, pool: Any = None, principal_kind: Literal["client", "operator"] = "operator") -> RoutingSnapshot:
    snapshot, _ = await _mutate(channel_id,
        lambda conn, capture: storage.set_bound_chats_only(channel_id, bound_only, conn=conn, before_write=capture),
        lambda old: replace(old, bound_only=bound_only), registry=registry, pool=pool,
        action="channel_chats.set_policy", principal_kind=principal_kind)
    return snapshot


async def remove_bot_with_routing(channel_id: str, *, principal_kind: Literal["client", "operator"] = "operator",
                                   refuse_if_routed: bool = False,
                                   registry: ChannelRegistry | None = None, pool: Any = None,
                                   ) -> Literal["deleted", "missing", "builtin", "has_bindings"]:
    """Delete under the mutation lock, preserving the running adapter's owner/policy.

    The optional client guard and cascade delete share the locked bot transaction.
    Correlations and old task delivery remain usable until the adapter restarts.
    """
    if principal_kind not in ("client", "operator"):
        raise ValueError("unknown principal kind")
    refuse_if_routed = refuse_if_routed or principal_kind == "client"
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
