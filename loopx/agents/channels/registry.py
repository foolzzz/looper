"""Channel registry: manages adapter lifecycle and outbound message routing."""

from __future__ import annotations

import logging
from typing import Any

from ..bus import MessageBus
from ..message import MessageEnvelope, MessageType
from .base import ChannelAdapter, DeliveryReceipt

logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Manages channel adapter lifecycle and routes outbound messages."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._channels: dict[str, ChannelAdapter] = {}
        self._setup_outbound_routing()

    def _setup_outbound_routing(self) -> None:
        for msg_type in (MessageType.AGENT_REPLY, MessageType.STATUS_UPDATE, MessageType.CLARIFICATION_REQUEST):
            self._bus.subscribe_topic(msg_type, self._route_outbound)

    def _route_outbound(self, envelope: MessageEnvelope) -> None:
        origin = envelope.metadata.get("origin_channel_id")
        if origin and origin in self._channels:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._deliver_safe(origin, envelope))
            except RuntimeError:
                logger.warning("no event loop for outbound routing to %s", origin)

    async def _deliver_safe(self, channel_id: str, envelope: MessageEnvelope) -> None:
        adapter = self._channels.get(channel_id)
        if not adapter:
            return
        try:
            receipt = await adapter.deliver(envelope)
            if not receipt.success:
                logger.warning("delivery failed to %s: %s", channel_id, receipt.error)
        except Exception:
            logger.exception("error delivering to channel %s", channel_id)

    async def register(self, adapter: ChannelAdapter) -> None:
        self._channels[adapter.channel_id] = adapter
        await adapter.start(self._bus)
        logger.info("channel %s (%s) registered", adapter.channel_id, adapter.channel_type)

    async def unregister(self, channel_id: str) -> None:
        adapter = self._channels.pop(channel_id, None)
        if adapter:
            await adapter.stop()
            logger.info("channel %s unregistered", channel_id)

    async def stop_all(self) -> None:
        for channel_id in list(self._channels):
            await self.unregister(channel_id)

    def list_channels(self) -> list[dict[str, Any]]:
        return [
            {
                "channel_id": adapter.channel_id,
                "channel_type": adapter.channel_type,
                **adapter.healthcheck(),
            }
            for adapter in self._channels.values()
        ]
