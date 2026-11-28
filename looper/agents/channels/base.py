"""Channel adapter protocol and base utilities.

Each user-facing channel (Web UI, Telegram, Lark) implements this protocol
to bridge between channel-native events and the internal MessageEnvelope format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..bus import MessageBus
from ..message import MessageEnvelope, MessageType
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
    outcome: PostOutcome | None = None

    @property
    def result(self) -> PostOutcome:
        return PostOutcome.SENT if self.success else (self.outcome or PostOutcome.UNKNOWN)


class PostOutcome(StrEnum):
    """What posting a message did, as far as the poster can tell: ``SENT``, ``REJECTED`` (the platform answered, or the
    call never left, so nothing was sent: asking again is safe) or ``UNKNOWN`` (the message may have been accepted: a
    timeout, a lost connection, an answer that could not be read). An unknown post is never asked again."""

    SENT = "sent"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class NotifyAnswer:
    """``(sent, error)`` plus the :class:`PostOutcome`. A tuple cannot carry the outcome (a subtype may not add
    slots), so this unpacks and compares as that pair. A schedule retry reads ``outcome`` so a send that may have
    gone is not asked again."""

    __slots__ = ("outcome", "_pair")

    def __init__(self, outcome: PostOutcome, error: str | None = None) -> None:
        sent = outcome is PostOutcome.SENT
        self.outcome = outcome
        self._pair = (sent, None if sent else error)

    def __iter__(self):
        return iter(self._pair)

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int):
        return self._pair[index]

    @property
    def error(self) -> str | None:
        return self._pair[1]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NotifyAnswer):
            return self.outcome is other.outcome and self._pair == other._pair
        if isinstance(other, tuple):
            return self._pair == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._pair)

    def __repr__(self) -> str:
        return f"NotifyAnswer({self.outcome.value}, error={self._pair[1]!r})"


def notice_answer(success: bool, error: str | None, outcome: PostOutcome | None, *,
                  sent_any: bool = False) -> NotifyAnswer:
    """How a schedule retry reads a channel send.

    A missing outcome, or any part that already left, may have been accepted: ``UNKNOWN``, and it is not asked
    again. Only an explicit refusal before anything was sent is ``REJECTED`` and may be retried. ``notify`` itself
    stays a ``(sent, error)`` pair; the schedule path is the caller of this.
    """
    if success:
        return NotifyAnswer(PostOutcome.SENT)
    if sent_any or outcome is None or outcome is PostOutcome.SENT:
        return NotifyAnswer(PostOutcome.UNKNOWN, error)
    return NotifyAnswer(outcome, error)


@dataclass(frozen=True)
class PostReceipt:
    """What posting a message to a chat did: the ids of the channel messages sent (several when it was split), or
    why it failed, and whether that was because the message it replies to is gone (``parent_gone``: posting it on
    its own may work)."""

    success: bool
    external_ids: tuple[str, ...] = ()
    error: str | None = None
    parent_gone: bool = False
    # Set by an adapter that can tell a refusal from a lost answer; a failure that says nothing is ``UNKNOWN``.
    outcome: PostOutcome | None = None

    @property
    def result(self) -> PostOutcome:
        return PostOutcome.SENT if self.success else (self.outcome or PostOutcome.UNKNOWN)


class ChannelAdapter(ABC):
    """Bidirectional bridge between a user channel and the message bus.

    Adapters translate channel-specific events into MessageEnvelope and deliver
    outbound messages in the channel's native format. They contain no business
    logic — only protocol translation.
    """

    delivery_observer: Callable[[bool], None] | None = None

    def record_delivery(self, success: bool) -> None:
        if self.delivery_observer is not None:
            self.delivery_observer(success)

    def record_envelope_delivery(self, envelope: MessageEnvelope, receipt: DeliveryReceipt) -> None:
        from .status_line import StatusKind, status_kind

        # Phase updates and empty replies can succeed without sending anything. They cannot prove recovery.
        if receipt.failure is DeliveryFailure.NO_CHAT:
            return
        if envelope.message_type is MessageType.STATUS_UPDATE:
            if status_kind(envelope.payload) is not StatusKind.PROGRESS:
                return
        elif envelope.message_type is not MessageType.CHAT_NOTICE and not envelope.payload.get('text'):
            return
        self.record_delivery(receipt.success)

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

    def prepare_stop(self) -> None:
        """Persist shutdown state before any agent or channel drain can block."""

    @abstractmethod
    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Send a message from the system to the user through this channel."""
        ...

    async def notify(self, chat: str, text: str, *, idempotency_key: str | None = None) -> DeliveryReceipt:
        """Send ``text`` unprompted. A capable channel deduplicates caller retries by ``idempotency_key``."""
        return DeliveryReceipt(channel=self.channel_type, success=False,
                               error=f"the {self.channel_type} channel cannot send notifications")

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        """Send ``text``, unprompted, to ``target`` (a reply to the channel message ``reply_to`` when given) and return
        the ids of the messages sent, so replies to them can be routed back: a user gate's messages
        (``gate_courier``). A channel that can uses ``idempotency_key`` to send one message for it, however often it
        is asked."""
        return PostReceipt(False, error=f"the {self.channel_type} channel cannot post messages",
                           outcome=PostOutcome.REJECTED)

    async def post_card(self, target: NotifyTarget, card: Any, *, reply_to: str | None = None,
                        idempotency_key: str | None = None) -> PostReceipt:
        """Post ``card`` (``card_actions.Card``) like ``post``, with its buttons where this channel has them; a
        channel without buttons posts its text."""
        return await self.post(target, card.text, reply_to=reply_to, idempotency_key=idempotency_key)

    async def edit_card(self, target: NotifyTarget, external_id: str, card: Any) -> PostReceipt:
        """Update an existing card, removing its buttons when settled; text-only channels edit its text."""
        return await self.edit(target, external_id, card.text)

    async def edit(self, target: NotifyTarget, external_id: str, text: str) -> PostReceipt:
        """Edit a bot's own message when this channel supports it."""
        return PostReceipt(False, error=f"the {self.channel_type} channel cannot edit messages",
                           outcome=PostOutcome.REJECTED)

    def healthcheck(self) -> dict[str, Any]:
        """Return channel health status. Override for richer diagnostics."""
        return {"channel": self.channel_type, "healthy": True}
