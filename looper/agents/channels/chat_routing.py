"""Process-local native ingress routing; only these service writes activate live changes.

A service registry activates the committed snapshot. Without one, helpers persist
choices for the next startup only. Direct SQL and sharing a live bot across servers
are not invalidation APIs. No egress caller should resolve a chat again.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from ...db import chat_bindings as storage
from ...db.chat_bindings import SenderPolicy
from ...db.channel_configs import BUILTIN_CHANNEL_IDS
from ...db.connection import get_pool
from ..message import channel_thread_id
from . import allowlist

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
              *, reason: str | None = None, outcome: str = "admitted", via_chat_senders: bool = False) -> None:
    silent = route.action is RouteAction.SILENT
    boundary_log(log, "chat_route", channel_id=channel_id, chat_id=chat_id,
                 manager_id=None if silent else route.manager_id, action=route.action.value,
                 reason=reason or ("only_bound_chats" if silent else "explicit_binding" if route.explicit else "bot_manager"),
                 outcome="silenced" if silent else outcome,
                 **({"sender_list": "chat_binding"} if via_chat_senders else {}),
                 **({"sender_policy": "chat"} if route.sender_policy is SenderPolicy.CHAT else {}))


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
    senders: frozenset[str] = frozenset()
    sender_policy: SenderPolicy = SenderPolicy.LISTED


ChatResolver = Callable[[str, str], RouteDecision]
ChatStop = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class RoutingSnapshot:
    channel_id: str
    bot_manager_id: str
    bound_only: bool
    bindings: Mapping[str, str]
    senders: Mapping[str, frozenset[str]] = field(default_factory=dict)
    sender_policies: Mapping[str, SenderPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.bound_only, bool):
            raise ValueError("bound_only must be a boolean")
        channel_thread_id(self.channel_id, "1", self.bot_manager_id, self.bot_manager_id)
        bindings = dict(self.bindings)
        for chat_id, manager_id in bindings.items():
            channel_thread_id(self.channel_id, chat_id, manager_id, self.bot_manager_id)
        senders = {}
        for chat_id, ids in self.senders.items():
            if chat_id not in bindings:
                raise ValueError("extra senders require an explicit chat binding")
            if frozen := frozenset(ids):
                senders[chat_id] = frozen
        policies = {}
        for chat_id, policy in self.sender_policies.items():
            if chat_id not in bindings:
                raise ValueError("sender policies require an explicit chat binding")
            policy = SenderPolicy(policy)
            if policy is SenderPolicy.CHAT:
                if storage._kind(self.channel_id) == "telegram":
                    raise ValueError("chat sender policy is only supported for Lark")
                policies[chat_id] = policy
        object.__setattr__(self, "sender_policies", MappingProxyType(policies))
        object.__setattr__(self, "bindings", MappingProxyType(bindings))
        object.__setattr__(self, "senders", MappingProxyType(senders))

    @classmethod
    def from_storage(cls, row: storage.RoutingSnapshot) -> RoutingSnapshot:
        return cls(row["channel_id"], row["manager_id"], row["bound_chats_only"],
                   {item["chat_id"]: item["manager_id"] for item in row["bindings"]},
                   {item["chat_id"]: frozenset(item["allowed_senders"]) for item in row["bindings"]},
                   {item["chat_id"]: SenderPolicy(item.get("sender_policy", "listed")) for item in row["bindings"]})

    def resolve(self, chat_id: str) -> RouteDecision:
        target = self.bindings.get(chat_id)
        return RouteDecision(RouteAction.SILENT if target is None and self.bound_only else RouteAction.ADMIT,
                             target if target is not None else self.bot_manager_id, target is not None,
                             senders=self.senders.get(chat_id, frozenset()),
                             sender_policy=self.sender_policies.get(chat_id, SenderPolicy.LISTED))


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
            if chat_id is not None:
                old_policy = before.sender_policies.get(chat_id, SenderPolicy.LISTED)
                new_policy = snapshot.sender_policies.get(chat_id, SenderPolicy.LISTED)
                if old_policy != new_policy:
                    values.update(old_sender_policy=old_policy.value, new_sender_policy=new_policy.value)
            if action == "manager_chat_bindings.put":
                old_count, new_count = len(before.senders.get(chat_id, ())), len(snapshot.senders.get(chat_id, ()))
                if old_count or new_count:
                    values.update(old_sender_count=old_count, new_sender_count=new_count)
            boundary_log(logger, "chat_routing_write", channel_id=channel_id, chat_id=chat_id,
                         action=action, principal_kind=principal_kind, outcome="committed", **values)

        await _commit_publish(transaction, registry, snapshot, committed)
        return snapshot, result


async def bind_chat(channel_id: str, chat_id: str, manager_id: str, *,
                    registry: ChannelRegistry | None = None, pool: Any = None, principal_kind: Literal["client", "operator"] = "operator",
                    readback: Callable[[Any], Awaitable[None]] | None = None,
                    allowed_senders: Sequence[str] | None = None,
                    sender_policy: SenderPolicy | str | None = None,
                    expected_manager_revision: int | None = None) -> RoutingSnapshot:
    """Capture any response under the same lock as the write, before commit/publication."""
    ids = (list(allowlist.sender_ids(storage._kind(channel_id), list(allowed_senders)))
           if allowed_senders is not None else None)
    stored: storage.ChatBinding

    async def change(conn: Any, capture: Any) -> None:
        nonlocal stored
        stored = await storage.put_binding(channel_id, chat_id, manager_id, conn=conn, before_write=capture,
                                           allowed_senders=ids, sender_policy=sender_policy,
                                           expected_manager_revision=expected_manager_revision)
        if readback is not None:
            await readback(conn)

    snapshot, _ = await _mutate(channel_id, change,
        lambda old: replace(old, bindings={**old.bindings, chat_id: manager_id},
            senders={**{k: v for k, v in old.senders.items() if k != chat_id},
                     chat_id: frozenset(stored["allowed_senders"])},
            sender_policies={**{k: v for k, v in old.sender_policies.items() if k != chat_id},
                             chat_id: SenderPolicy(stored["sender_policy"])}), registry=registry, pool=pool,
        action="manager_chat_bindings.put", principal_kind=principal_kind, chat_id=chat_id)
    return snapshot


async def unbind_chat(channel_id: str, chat_id: str, *,
                      registry: ChannelRegistry | None = None, pool: Any = None, principal_kind: Literal["client", "operator"] = "operator") -> bool:
    _, deleted = await _mutate(channel_id,
        lambda conn, capture: storage.delete_binding(channel_id, chat_id, conn=conn, before_write=capture),
        lambda old: replace(old, bindings={key: value for key, value in old.bindings.items() if key != chat_id},
                            senders={key: value for key, value in old.senders.items() if key != chat_id},
                            sender_policies={key: value for key, value in old.sender_policies.items() if key != chat_id}),
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
