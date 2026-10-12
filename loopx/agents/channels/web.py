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
from pathlib import Path
from typing import Any, AsyncIterator

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


class WebChannelAdapter(ChannelAdapter):
    """Bridges Web UI HTTP/SSE with the agent message bus."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8767) -> None:
        self._host = host
        self._port = port
        self._bus: MessageBus | None = None
        self._address = channel_address("web", "channel-web")
        self._sse_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
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
        corr_id = envelope.correlation_id
        sse_q = self._sse_queues.get(corr_id)

        if sse_q is None:
            return DeliveryReceipt(
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
    ) -> dict[str, str]:
        assert self._bus is not None
        correlation_id = str(uuid.uuid4())

        sse_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._sse_queues[correlation_id] = sse_q

        envelope = MessageEnvelope.create(
            source=self._address,
            target=MANAGER_ADDRESS,
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
