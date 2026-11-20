"""Channel registry: manages adapter lifecycle.

Outbound messages are addressed to a channel id (``channel-<type>`` or ``channel-<type>-<n>``) and each
adapter delivers what arrives on its own bus queue; the registry does not route
them a second time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import turn_metrics
from ..turn_metrics import ProactiveRef
from ..bus import MessageBus
from ..message import parse_channel_id
from ..task_notice import NotifyTarget
from ..ops.leads import for_group
from .base import ChannelAdapter, PostOutcome, PostReceipt
from .chat_routing import RouteDecision, RoutingSnapshot, load_snapshot
from .lark_quote import QuoteRead, QuoteReader, QuoteRef, QuoteUnavailable

logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Manages channel adapter lifecycle."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._channels: dict[str, ChannelAdapter] = {}
        self._routing: dict[str, RoutingSnapshot] = {}
        self._mutation_locks: dict[str, asyncio.Lock] = {}

    def mutation_lock(self, channel_id: str) -> asyncio.Lock:
        return self._mutation_locks.setdefault(channel_id, asyncio.Lock())

    def bindings_changed(self, channel_id: str, snapshot: RoutingSnapshot) -> None:
        """Total, synchronous publication: no I/O, callbacks or adapter dependency."""
        self._routing[channel_id] = snapshot

    def resolve_chat(self, channel_id: str, chat_id: str) -> RouteDecision:
        return self._routing[channel_id].resolve(chat_id)

    async def register(self, adapter: ChannelAdapter) -> None:
        if parse_channel_id(adapter.channel_id) is not None:
            async with self.mutation_lock(adapter.channel_id):
                try:
                    snapshot = await load_snapshot(adapter.channel_id)
                except Exception:
                    logger.exception("%s not started: cannot load chat routing", adapter.channel_id)
                    raise
                self.bindings_changed(adapter.channel_id, snapshot)
                adapter._resolve_chat = self.resolve_chat
        self._channels[adapter.channel_id] = adapter
        await adapter.start(self._bus)
        logger.info("channel %s (%s) registered", adapter.channel_id, adapter.channel_type)

    async def unregister(self, channel_id: str) -> None:
        adapter = self._channels.pop(channel_id, None)
        if adapter:
            await adapter.stop()
            logger.info("channel %s unregistered", channel_id)

    def prepare_stop_all(self) -> None:
        for adapter in self._channels.values():
            adapter.prepare_stop()

    async def stop_all(self) -> None:
        for channel_id in list(self._channels):
            await self.unregister(channel_id)

    def get(self, channel_id: str) -> ChannelAdapter | None:
        return self._channels.get(channel_id)

    @staticmethod
    def _not_running(channel_id: str) -> str:
        parsed = parse_channel_id(channel_id)
        if parsed is not None and parsed.builtin:
            return f"the {parsed.channel_type} channel is not running: enable it in Settings → Channels"
        return f"the bot {channel_id} is not running: enable it in Settings → Channels"

    async def notify(self, channel_id: str, chat: str, text: str, *,
                     proactive: ProactiveRef | None = None, group: bool = False) -> tuple[bool, str | None]:
        """Send a schedule's message through the running channel ``channel_id``: whether it was sent, and why
        not."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=chat,
                                              group=group, external_id=None,
                                              outcome=PostOutcome.REJECTED.value)
            return False, self._not_running(channel_id)
        try:
            receipt = await adapter.notify(chat, text)
        except Exception:
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=chat, group=group,
                                              external_id=None, outcome=PostOutcome.UNKNOWN.value)
            raise
        if proactive is not None:
            turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=chat,
                                          group=group,
                                          external_id=receipt.external_id, outcome=receipt.result.value)
        return receipt.success, receipt.error

    async def post(self, channel_id: str, target: NotifyTarget, text: str, *,
                   reply_to: str | None = None, idempotency_key: str | None = None,
                   proactive: ProactiveRef | None = None) -> PostReceipt:
        """Post ``text`` to ``target`` through the running channel ``channel_id`` (``ChannelAdapter.post``)."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                              external_id=None, outcome=PostOutcome.REJECTED.value)
            return PostReceipt(False, error=self._not_running(channel_id), outcome=PostOutcome.REJECTED)
        if target.group:
            text = for_group(text)
        try:
            receipt = await adapter.post(target, text, reply_to=reply_to, idempotency_key=idempotency_key)
        except Exception:
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                              external_id=None, outcome=PostOutcome.UNKNOWN.value)
            raise
        if proactive is not None:
            turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                          external_id=receipt.external_ids[0] if receipt.external_ids else None,
                                          outcome=receipt.result.value)
        return receipt

    async def edit(self, channel_id: str, target: NotifyTarget, external_id: str, text: str) -> PostReceipt:
        """Edit one message through its running adapter, preserving the platform's delivery result."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            return PostReceipt(False, error=self._not_running(channel_id), outcome=PostOutcome.REJECTED)
        return await adapter.edit(target, external_id, text)

    async def read_quote(self, ref: QuoteRef, *, after: int) -> QuoteRead:
        """What a received message quotes, read by the running channel that received it (``QuoteReader``); raises
        ``QuoteUnavailable`` when that channel is not running or reads no quotes."""
        adapter = self._channels.get(ref.channel_id)
        if not isinstance(adapter, QuoteReader):
            raise QuoteUnavailable(f"{ref.channel_id} is not running" if adapter is None
                                   else f"{ref.channel_id} reads no quotes")
        return await adapter.read_quote(ref, after=after)

    def list_channels(self) -> list[dict[str, Any]]:
        return [
            {
                "channel_id": adapter.channel_id,
                "channel_type": adapter.channel_type,
                **adapter.healthcheck(),
            }
            for adapter in self._channels.values()
        ]
