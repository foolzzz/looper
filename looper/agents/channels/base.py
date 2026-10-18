"""Channel adapter protocol and base utilities.

Each user-facing channel (Web UI, Telegram, Lark) implements this protocol
to bridge between channel-native events and the internal MessageEnvelope format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable

from ..bus import MessageBus
from ..message import MessageEnvelope
from ..task_notice import NotifyTarget


class DeliveryFailure(StrEnum):
    """Why a channel did not deliver an answer; stored with it (``chat_history.record_reply``)."""

    NO_CHAT = "no_chat"  # the channel does not know the chat the message came from (it restarted meanwhile)
    SEND_FAILED = "send_failed"  # the channel tried and could not send it


@dataclass
class DeliveryReceipt:
    """Proof that a message was delivered to the channel."""

    channel: str
    success: bool
    external_id: str | None = None
    error: str | None = None
    failure: DeliveryFailure | None = None  # when not ``success``: why, if not SEND_FAILED


@dataclass(frozen=True)
class PostReceipt:
    """What posting a message to a chat did: the ids of the channel messages sent (several when it was split), or
    why it failed, and whether that was because the message it replies to is gone (``parent_gone``: posting it on
    its own may work)."""

    success: bool
    external_ids: tuple[str, ...] = ()
    error: str | None = None
    parent_gone: bool = False


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

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        """Send ``text``, unprompted, to ``target`` (a reply to the channel message ``reply_to`` when given) and return
        the ids of the messages sent, so replies to them can be routed back: a user gate's messages
        (``gate_courier``). A channel that can uses ``idempotency_key`` to send one message for it, however often it
        is asked."""
        return PostReceipt(False, error=f"the {self.channel_type} channel cannot post messages")

    def healthcheck(self) -> dict[str, Any]:
        """Return channel health status. Override for richer diagnostics."""
        return {"channel": self.channel_type, "healthy": True}
