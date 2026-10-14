"""Lark (Feishu) Bot Channel Adapter.

Bridges Lark Bot messages with the agent Bus through ``lark-cli``:
``lark-cli event consume`` delivers the incoming message events and
``lark-cli message send`` posts the replies.

This adapter is intentionally thin — it translates between Bus
MessageEnvelopes and those lark-cli calls rather than reimplementing the
Lark API surface.

Only the senders on its allowlist (their ``open_id``) reach the Manager:
anyone else is dropped with one log line and gets no reply, and an empty
list keeps the adapter from starting.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ..bus import MessageBus
from ..message import (
    MANAGER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    channel_address,
)
from .allowlist import SenderAllowlist
from .base import ChannelAdapter, DeliveryReceipt

logger = logging.getLogger(__name__)


def _check_lark_cli_available() -> bool:
    """Check if lark-cli is available for event collection."""
    import shutil
    return bool(shutil.which("lark-cli"))


class LarkChannelAdapter(ChannelAdapter):
    """Bridges Lark Bot with the agent message bus.

    Reuses the existing lark-cli subprocess event collection and
    lark-cli send for outbound delivery. The adapter translates
    between Lark event JSON and MessageEnvelope format.
    """

    def __init__(
        self,
        *,
        allowlist: SenderAllowlist,
        app_id: str | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        self._allowlist = allowlist  # open_ids
        self._app_id = app_id or os.environ.get("LARK_APP_ID", "")
        self._runtime_root = runtime_root or Path.cwd()
        self._bus: MessageBus | None = None
        self._address = channel_address("lark", "channel-lark")
        self._outbound_task: asyncio.Task[Any] | None = None
        self._collector_task: asyncio.Task[Any] | None = None
        self._running = False
        # correlation_id → lark chat_id for reply routing
        self._correlation_chat: dict[str, str] = {}

    @property
    def channel_type(self) -> str:
        return "lark"

    @property
    def channel_id(self) -> str:
        return "channel-lark"

    async def start(self, bus: MessageBus) -> None:
        if not self._allowlist.ids:
            # The Manager runs any command it is asked to: nobody may reach it unlisted.
            logger.error("channel-lark not started: its sender allowlist is empty")
            return

        if not _check_lark_cli_available():
            logger.warning(
                "lark-cli not found, lark channel adapter disabled. "
                "Install lark-cli to enable Lark integration."
            )
            return

        if not self._app_id:
            logger.warning("LARK_APP_ID not set, lark channel adapter disabled")
            return

        self._bus = bus
        self._running = True
        self._outbound_task = asyncio.create_task(self._outbound_loop())
        self._collector_task = asyncio.create_task(self._event_collector_loop())
        logger.info("lark channel adapter started (app_id=%s)", self._app_id)

    async def stop(self) -> None:
        self._running = False
        for task in (self._outbound_task, self._collector_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("lark channel adapter stopped")

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Send a reply to the Lark chat that originated the conversation."""
        corr_id = envelope.correlation_id
        chat_id = self._correlation_chat.get(corr_id)
        if chat_id is None:
            return DeliveryReceipt(
                channel="lark",
                success=False,
                error=f"no chat_id for correlation {corr_id}",
            )

        text = self._format_reply(envelope)
        if not text:
            return DeliveryReceipt(channel="lark", success=True)

        try:
            await self._send_lark_message(chat_id, text)
            return DeliveryReceipt(channel="lark", success=True)
        except Exception as exc:
            logger.warning("lark delivery failed: %s", exc)
            return DeliveryReceipt(channel="lark", success=False, error=str(exc))

    def healthcheck(self) -> dict[str, Any]:
        return {
            "channel": "lark",
            "healthy": self._running,
            "lark_cli_available": _check_lark_cli_available(),
            "active_conversations": len(self._correlation_chat),
        }

    # --- Inbound: lark-cli events → Bus ---

    async def _event_collector_loop(self) -> None:
        """Run lark-cli event consume as a subprocess and process events."""
        while self._running:
            try:
                await self._run_event_consumer()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("lark event collector crashed, restarting in 10s")
                await asyncio.sleep(10)

    async def _run_event_consumer(self) -> None:
        """Start lark-cli event consume subprocess and read events from stdout."""
        command = [
            "lark-cli", "event", "consume",
            "--app-id", self._app_id,
            "--format", "json",
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        assert process.stdout is not None
        try:
            async for raw_line in process.stdout:
                if not self._running:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = __import__("json").loads(line)
                    await self._handle_lark_event(event)
                except (__import__("json").JSONDecodeError, Exception):
                    logger.debug("skipping non-JSON lark event line")
        finally:
            if process.returncode is None:
                process.kill()

    async def _handle_lark_event(self, event: dict[str, Any]) -> None:
        """Process a single Lark event and publish to Bus if it's a user message."""
        assert self._bus is not None
        import uuid as _uuid

        event_type = event.get("type", "")
        if event_type != "im.message.receive_v1":
            return

        msg = event.get("event", {}).get("message", {})
        chat_id = msg.get("chat_id", "")
        msg_type = msg.get("message_type", "")
        sender = event.get("event", {}).get("sender", {}).get("sender_id", {})

        # Before anything else: an unlisted sender is logged and never answered.
        if not self._allowlist.admits(sender.get("open_id")):
            return

        if msg_type != "text":
            return

        content = msg.get("content", "{}")
        try:
            text = __import__("json").loads(content).get("text", "")
        except (__import__("json").JSONDecodeError, AttributeError):
            text = content

        if not text.strip():
            return

        # Remove bot @mention prefix if present
        text = self._strip_mention(text)
        if not text.strip():
            return

        correlation_id = str(_uuid.uuid4())
        self._correlation_chat[correlation_id] = chat_id

        envelope = MessageEnvelope.create(
            source=self._address,
            target=MANAGER_ADDRESS,
            message_type=MessageType.USER_MESSAGE,
            payload={
                "text": text,
                "language": "zh",
                "channel_context": {
                    "chat_id": chat_id,
                    "sender_id": sender.get("open_id", ""),
                    "message_id": msg.get("message_id", ""),
                },
            },
            correlation_id=correlation_id,
            metadata={"origin_channel_id": self.channel_id},
        )
        await self._bus.publish(envelope)

    # --- Outbound: Bus → Lark ---

    async def _outbound_loop(self) -> None:
        assert self._bus is not None
        try:
            async for envelope in self._bus.subscribe(self.channel_id):
                if not self._running:
                    break
                await self.deliver(envelope)
        except asyncio.CancelledError:
            pass

    async def _send_lark_message(self, chat_id: str, text: str) -> None:
        """Send a text message to a Lark chat using lark-cli."""
        import json as _json

        content = _json.dumps({"text": text}, ensure_ascii=False)
        command = [
            "lark-cli", "message", "send",
            "--app-id", self._app_id,
            "--chat-id", chat_id,
            "--msg-type", "text",
            "--content", content,
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            err = stderr.decode("utf-8", errors="replace") if stderr else ""
            raise RuntimeError(f"lark-cli send failed (rc={process.returncode}): {err[:200]}")

    @staticmethod
    def _format_reply(envelope: MessageEnvelope) -> str:
        match envelope.message_type:
            case MessageType.AGENT_REPLY | MessageType.CLARIFICATION_REQUEST:
                return envelope.payload.get("text", "")
            case MessageType.STATUS_UPDATE:
                text = envelope.payload.get("text", "")
                if envelope.payload.get("delta"):
                    return ""
                return text
            case _:
                return ""

    @staticmethod
    def _strip_mention(text: str) -> str:
        """Remove @bot mention prefix from incoming messages."""
        import re
        text = re.sub(r"@\S+\s*", "", text, count=1).strip()
        return text
