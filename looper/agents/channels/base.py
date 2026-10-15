"""Channel adapter protocol and base utilities.

Each user-facing channel (Web UI, Telegram, Lark) implements this protocol
to bridge between channel-native events and the internal MessageEnvelope format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..bus import MessageBus
from ..message import MessageEnvelope


@dataclass
class DeliveryReceipt:
    """Proof that a message was delivered to the channel."""

    channel: str
    success: bool
    external_id: str | None = None
    error: str | None = None


class ChannelAdapter(ABC):
    """Bidirectional bridge between a user channel and the message bus.

    Adapters translate channel-specific events into MessageEnvelope and deliver
    outbound messages in the channel's native format. They contain no business
    logic — only protocol translation.
    """

    @property
    @abstractmethod
    def channel_type(self) -> str:
        """Channel identifier: 'web', 'telegram', 'lark'."""
        ...

    @property
    @abstractmethod
    def channel_id(self) -> str:
        """Unique instance identifier for this channel."""
        ...

    @abstractmethod
    async def start(self, bus: MessageBus) -> None:
        """Begin listening for inbound messages and register with the bus."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the channel."""
        ...

    @abstractmethod
    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Send a message from the system to the user through this channel."""
        ...

    async def notify(self, chat: str, text: str) -> DeliveryReceipt:
        """Send ``text``, unprompted, to ``chat``: a schedule's message, or a task's end (``task_notice``)."""
        return DeliveryReceipt(channel=self.channel_type, success=False,
                               error=f"the {self.channel_type} channel cannot send notifications")

    def healthcheck(self) -> dict[str, Any]:
        """Return channel health status. Override for richer diagnostics."""
        return {"channel": self.channel_type, "healthy": True}
