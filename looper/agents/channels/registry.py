"""Channel registry: manages adapter lifecycle.

Outbound messages are addressed to a channel (``channel-<name>``) and each
adapter delivers what arrives on its own bus queue; the registry does not route
them a second time.
"""

from __future__ import annotations

import logging
from typing import Any

from ..bus import MessageBus
from ..task_notice import NotifyTarget
from .base import ChannelAdapter, PostReceipt

logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Manages channel adapter lifecycle."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._channels: dict[str, ChannelAdapter] = {}

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

    def get(self, channel_id: str) -> ChannelAdapter | None:
        return self._channels.get(channel_id)

    async def notify(self, channel_type: str, chat: str, text: str) -> tuple[bool, str | None]:
        """Send a schedule's message through the running ``channel_type`` channel: whether it was sent, and why
        not."""
        adapter = self._channels.get(f"channel-{channel_type}")
        if adapter is None:
            return False, f"the {channel_type} channel is not running: enable it in Settings → Channels"
        receipt = await adapter.notify(chat, text)
        return receipt.success, receipt.error

    async def post(self, channel_type: str, target: NotifyTarget, text: str, *,
                   reply_to: str | None = None) -> PostReceipt:
        """Post ``text`` to ``target`` through the running ``channel_type`` channel (``ChannelAdapter.post``)."""
        adapter = self._channels.get(f"channel-{channel_type}")
        if adapter is None:
            return PostReceipt(False, error=f"the {channel_type} channel is not running: enable it in Settings → "
                                            "Channels")
        return await adapter.post(target, text, reply_to=reply_to)

    def list_channels(self) -> list[dict[str, Any]]:
        return [
            {
                "channel_id": adapter.channel_id,
                "channel_type": adapter.channel_type,
                **adapter.healthcheck(),
            }
            for adapter in self._channels.values()
        ]
