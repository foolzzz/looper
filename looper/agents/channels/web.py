"""Web UI Channel Adapter.

Bridges the HTTP frontend with the agent Bus via async message queues.
Inbound: HTTP POST → MessageEnvelope → Bus
Outbound: Bus → SSE stream events
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator

from ..broadcast import Broadcaster
from ..bus import MessageBus
from ..message import (
    MANAGER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    channel_address,
)
from .base import ChannelAdapter, DeliveryReceipt

logger = logging.getLogger(__name__)


# The messages that end a message's stream with its answer (``_envelope_to_sse_event``'s ``answer.final``).
_ANSWERS = (MessageType.AGENT_REPLY, MessageType.CLARIFICATION_REQUEST)
# At most this many messages wait for their answer to be saved; the oldest is forgotten past it.
AWAITING_MAX = 1000


def reply_message_id(message_id: str) -> str:
    """The chat id of the answer to the chat message ``message_id``; the Chat page streams it under the same id."""
    return f"{message_id}:reply"


class WebChannelAdapter(ChannelAdapter):
    """Bridges Web UI HTTP/SSE with the agent message bus."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8767) -> None:
        self._host = host
        self._port = port
        self._bus: MessageBus | None = None
        self._address = channel_address("web", "channel-web")
        self._sse_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        # The chat of each message still waiting for its answer, by correlation id: (chat session, message id). The
        # answer is saved to that chat whether or not a page still reads the message's stream (``deliver``).
        self._awaiting: OrderedDict[str, tuple[str, str]] = OrderedDict()
        # Open chat pages, keyed by chat session: what reaches a chat outside any message's stream.
        self._chats = Broadcaster()
        self._outbound_task: asyncio.Task[Any] | None = None
        self._running = False

    @property
    def channel_type(self) -> str:
        return "web"

    @property
    def channel_id(self) -> str:
        return "channel-web"

    async def start(self, bus: MessageBus) -> None:
        self._bus = bus
        self._running = True
        self._outbound_task = asyncio.create_task(self._outbound_loop())
        logger.info("web channel adapter started")

    async def stop(self) -> None:
        self._running = False
        if self._outbound_task:
            self._outbound_task.cancel()
            try:
                await self._outbound_task
            except asyncio.CancelledError:
                pass
        logger.info("web channel adapter stopped")

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        chat_session = envelope.payload.get("chat_session_id")
        if envelope.message_type == MessageType.AGENT_REPLY and isinstance(chat_session, str) and chat_session:
            return await self._add_to_chat(chat_session, envelope)

        corr_id = envelope.correlation_id
        saved: DeliveryReceipt | None = None
        if envelope.message_type in _ANSWERS and (awaiting := self._awaiting.pop(corr_id, None)):
            # Saved here, not by the page: a page left before the answer came would lose it.
            session_id, message_id = awaiting
            saved = await self._add_to_chat(session_id, envelope, message_id=reply_message_id(message_id))
        sse_q = self._sse_queues.get(corr_id)

        if sse_q is None:
            return saved or DeliveryReceipt(
                channel="web",
                success=False,
                error=f"no active SSE stream for correlation {corr_id}",
            )

        event = self._envelope_to_sse_event(envelope)
        try:
            sse_q.put_nowait(event)
            return DeliveryReceipt(channel="web", success=True)
        except asyncio.QueueFull:
            return DeliveryReceipt(
                channel="web", success=False, error="SSE queue full"
            )

    async def _add_to_chat(self, session_id: str, envelope: MessageEnvelope, *,
                           message_id: str | None = None) -> DeliveryReceipt:
        """Add a reply to its chat: one that answers no message (what the Manager's Claude said on its own), or a
        message's answer (``message_id`` its ``reply_message_id``).

        A message's stream ends at its answer, so a reply of Claude's own never
        goes to one. Either is saved with the chat's other messages, then pushed
        as a ``chat.message`` event to every page that has the chat open
        (``iter_chat_events``), even when saving failed; a page that streams the
        answer already shows it under the same id.
        """
        from ...db.chat import save_message

        text = str(envelope.payload.get("text") or "")
        message_id = message_id or envelope.message_id
        receipt = DeliveryReceipt(channel="web", success=True)
        try:
            await save_message(session_id=session_id, message_id=message_id, role="assistant", content=text)
        except Exception as exc:
            logger.warning("web: could not add a reply to chat %s", session_id, exc_info=True)
            receipt = DeliveryReceipt(channel="web", success=False, error=f"could not save to chat: {exc}")
        self._chats.publish(session_id, {
            "kind": "chat.message",
            "payload": {"message_id": message_id, "role": "assistant", "content": text,
                        "created_at": envelope.timestamp},
        })
        return receipt

    def iter_chat_events(self, session_id: str, *, heartbeat: float = 15.0) -> AsyncGenerator[dict[str, Any], None]:
        """The events of the chat ``session_id`` that belong to no message's stream, for as long as it is read.

        Subscribes at the call, so nothing added after it is missed; the
        stream yields a ``heartbeat`` when nothing came for ``heartbeat``
        seconds and unsubscribes once it is closed.
        """
        queue = self._chats.subscribe(session_id)

        async def events() -> AsyncGenerator[dict[str, Any], None]:
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    except asyncio.TimeoutError:
                        yield {"kind": "heartbeat", "payload": {}}
                        continue
                    if event is None:
                        return
                    yield event
            finally:
                self._chats.unsubscribe(session_id, queue)

        return events()

    def healthcheck(self) -> dict[str, Any]:
        return {
            "channel": "web",
            "healthy": self._running,
            "active_sse_streams": len(self._sse_queues),
        }

    # --- Inbound: HTTP → Bus ---

    async def handle_user_message(
        self,
        *,
        text: str,
        session_id: str | None = None,
        goal_id: str | None = None,
        language: str = "zh",
        attachments: list[dict[str, Any]] | None = None,
        target: AgentAddress = MANAGER_ADDRESS,
        message_id: str | None = None,
    ) -> dict[str, str]:
        """Send a chat message (``message_id``, in the chat ``session_id``) to its agent; its answer is saved to
        that chat under ``reply_message_id(message_id)``."""
        assert self._bus is not None
        correlation_id = str(uuid.uuid4())

        sse_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._sse_queues[correlation_id] = sse_q
        if session_id and message_id:
            self._awaiting[correlation_id] = (session_id, message_id)
            while len(self._awaiting) > AWAITING_MAX:  # answers that never came
                self._awaiting.popitem(last=False)

        envelope = MessageEnvelope.create(
            source=self._address,
            target=target,
            message_type=MessageType.USER_MESSAGE,
            payload={
                "text": text,
                "language": language,
                "attachments": attachments or [],
                "channel_context": {
                    "session_id": session_id,
                    "goal_id": goal_id,
                },
            },
            correlation_id=correlation_id,
            metadata={"origin_channel_id": self.channel_id},
        )
        await self._bus.publish(envelope)

        return {
            "correlation_id": correlation_id,
            "events_url": f"/api/chat/events?cid={correlation_id}",
        }

    async def iter_sse_events(
        self,
        correlation_id: str,
        timeout: float = 300.0,
    ) -> AsyncIterator[dict[str, Any]]:
        sse_q = self._sse_queues.get(correlation_id)
        if sse_q is None:
            return

        deadline = asyncio.get_event_loop().time() + timeout
        heartbeat_interval = 15.0
        last_heartbeat = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                event = await asyncio.wait_for(sse_q.get(), timeout=min(1.0, remaining))
                yield event
                if event.get("kind") in ("answer.final", "turn.completed", "turn.failed"):
                    break
            except asyncio.TimeoutError:
                now = asyncio.get_event_loop().time()
                if now - last_heartbeat >= heartbeat_interval:
                    yield {"kind": "heartbeat", "payload": {}}
                    last_heartbeat = now

        self._sse_queues.pop(correlation_id, None)

    def close_sse_stream(self, correlation_id: str) -> None:
        self._sse_queues.pop(correlation_id, None)

    # --- Outbound: Bus → SSE ---

    async def _outbound_loop(self) -> None:
        assert self._bus is not None
        try:
            async for envelope in self._bus.subscribe(self.channel_id):
                if not self._running:
                    break
                await self.deliver(envelope)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _envelope_to_sse_event(envelope: MessageEnvelope) -> dict[str, Any]:
        match envelope.message_type:
            case MessageType.AGENT_REPLY:
                return {
                    "kind": "answer.final",
                    "payload": {
                        "text": envelope.payload.get("text", ""),
                        "response": {"message": envelope.payload.get("text", "")},
                    },
                }
            case MessageType.STATUS_UPDATE:
                if envelope.payload.get("delta"):
                    return {
                        "kind": "answer.delta",
                        "payload": {"text": envelope.payload.get("text", "")},
                    }
                return {
                    "kind": "agent.phase",
                    "payload": {"label": envelope.payload.get("text", "")},
                }
            case MessageType.CLARIFICATION_REQUEST:
                return {
                    "kind": "answer.final",
                    "payload": {
                        "text": envelope.payload.get("text", ""),
                        "response": {"message": envelope.payload.get("text", "")},
                    },
                }
            case _:
                return {
                    "kind": "system.event",
                    "payload": {
                        "type": envelope.message_type.value,
                        **envelope.payload,
                    },
                }
