"""Channel registry: manages adapter lifecycle.

Outbound messages are addressed to a channel id (``channel-<type>`` or ``channel-<type>-<n>``) and each
adapter delivers what arrives on its own bus queue; the registry does not route
them a second time.
"""

from __future__ import annotations

import logging
from typing import Any

from ..bus import MessageBus
from ..message import parse_channel_id
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

    @staticmethod
    def _not_running(channel_id: str) -> str:
        parsed = parse_channel_id(channel_id)
        if parsed is not None and parsed.builtin:
            return f"the {parsed.channel_type} channel is not running: enable it in Settings → Channels"
        return f"the bot {channel_id} is not running: enable it in Settings → Channels"

    async def notify(self, channel_id: str, chat: str, text: str) -> tuple[bool, str | None]:
        """Send a schedule's message through the running channel ``channel_id``: whether it was sent, and why
        not."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            return False, self._not_running(channel_id)
        receipt = await adapter.notify(chat, text)
        return receipt.success, receipt.error

    async def post(self, channel_id: str, target: NotifyTarget, text: str, *,
                   reply_to: str | None = None, idempotency_key: str | None = None) -> PostReceipt:
        """Post ``text`` to ``target`` through the running channel ``channel_id`` (``ChannelAdapter.post``)."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            return PostReceipt(False, error=self._not_running(channel_id))
        return await adapter.post(target, text, reply_to=reply_to, idempotency_key=idempotency_key)

    def list_channels(self) -> list[dict[str, Any]]:
        return [
            {
                "channel_id": adapter.channel_id,
                "channel_type": adapter.channel_type,
                **adapter.healthcheck(),
            }
            for adapter in self._channels.values()
        ]
