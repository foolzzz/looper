"""Lark (Feishu) Bot Channel Adapter.

Bridges Lark Bot messages with the agent Bus through ``lark-cli`` (the
commands of lark-cli 1.0.97): ``lark-cli event consume im.message.receive_v1
--as bot`` streams the incoming messages, one flat JSON object per line
(``lark-cli event schema im.message.receive_v1``), and ``lark-cli im
+messages-send --as bot`` posts the replies to their chat and a schedule's
messages to a user (``send_command``). lark-cli acts as the app of its own
configuration (``lark-cli whoami``); ``--app-id`` is no longer a flag of it.

This adapter is intentionally thin — it translates between Bus
MessageEnvelopes and those lark-cli calls rather than reimplementing the
Lark API surface.

Only the senders on its allowlist (their ``open_id``) reach the Manager:
anyone else is dropped with one log line and gets no reply, and an empty
list keeps the adapter from starting.
"""

from __future__ import annotations

import asyncio
import json
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


# The event the adapter consumes: a message sent to the bot.
RECEIVE_EVENT = "im.message.receive_v1"


def consume_command() -> list[str]:
    """The lark-cli argv that streams the messages the bot receives, one JSON object per line."""
    return ["lark-cli", "event", "consume", RECEIVE_EVENT, "--as", "bot"]


def send_command(text: str, *, chat_id: str | None = None, user_id: str | None = None) -> list[str]:
    """The lark-cli argv that sends ``text`` as the bot to a chat (``oc_...``) or to a user (an open_id,
    ``ou_...``), exactly one of them."""
    if (chat_id is None) == (user_id is None):
        raise ValueError("send to exactly one of a chat and a user")
    target = ["--chat-id", chat_id] if chat_id is not None else ["--user-id", user_id]
    return ["lark-cli", "im", "+messages-send", "--as", "bot", *target, "--text", text]


def _failure(stderr: bytes) -> str:
    """lark-cli's reason for a failed command: its JSON error's message, else the tail of what it printed."""
    text = stderr.decode("utf-8", errors="replace").strip()
    try:
        message = (json.loads(text).get("error") or {}).get("message")
    except (ValueError, AttributeError):
        message = None
    return str(message) if message else text[-300:]


async def _run_send(argv: list[str]) -> None:
    """Run a send command; raises ``RuntimeError`` with lark-cli's reason when it fails."""
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"lark-cli exited {process.returncode}: {_failure(stderr or b'')}")


def _check_lark_cli_available() -> bool:
    """Check if lark-cli is available for event collection."""
    import shutil
    return bool(shutil.which("lark-cli"))


class LarkChannelAdapter(ChannelAdapter):
    """Bridges Lark Bot with the agent message bus.

    Collects events with ``lark-cli event consume`` and sends with
    ``lark-cli im +messages-send``; it translates between lark-cli's event
    JSON and MessageEnvelope format.
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

    async def notify(self, chat: str, text: str) -> DeliveryReceipt:
        """Send ``text`` as the bot to Lark user ``chat`` (an open_id)."""
        try:
            await _run_send(send_command(text, user_id=chat))
        except (OSError, RuntimeError) as exc:
            logger.warning("lark notification to %s failed: %s", chat, exc)
            return DeliveryReceipt(channel="lark", success=False, error=str(exc))
        return DeliveryReceipt(channel="lark", success=True)

    def healthcheck(self) -> dict[str, Any]:
        return {
            "channel": "lark",
            "healthy": self._running,
            "lark_cli_available": _check_lark_cli_available(),
            "active_conversations": len(self._correlation_chat),
        }

    # --- Inbound: lark-cli events → Bus ---

    async def _event_collector_loop(self) -> None:
        """Run lark-cli event consume as a subprocess and process events; start it again 10 s after it stops
        (a consumer that fails at once, such as one not signed in, is not restarted in a tight loop)."""
        while self._running:
            try:
                await self._run_event_consumer()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("lark event collector crashed, restarting in 10s")
            else:
                if self._running:
                    logger.warning("lark event consumer stopped; starting it again in 10s")
            await asyncio.sleep(10)

    async def _run_event_consumer(self) -> None:
        """Start lark-cli event consume subprocess and read events from stdout."""
        process = await asyncio.create_subprocess_exec(
            *consume_command(),
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
                    event = json.loads(line)
                    await self._handle_lark_event(event)
                except Exception:
                    logger.debug("skipping non-JSON lark event line")
        finally:
            if process.returncode is None:
                process.kill()

    async def _handle_lark_event(self, event: dict[str, Any]) -> None:
        """Publish a received text message to the Bus. The event is lark-cli's flat one: ``chat_id``,
        ``message_id``, ``message_type``, ``sender_id`` (the open_id), ``sender_type`` and ``content``, a text
        message's text."""
        assert self._bus is not None
        import uuid as _uuid

        if event.get("type", "") != RECEIVE_EVENT:
            return
        chat_id = event.get("chat_id", "")
        sender_open_id = event.get("sender_id")

        # Before anything else: an unlisted sender is logged and never answered.
        if not self._allowlist.admits(sender_open_id):
            return

        if event.get("sender_type") == "bot" or event.get("message_type", "") != "text":
            return

        text = event.get("content") or ""
        if not isinstance(text, str) or not text.strip():
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
                    "sender_id": sender_open_id or "",
                    "message_id": event.get("message_id", ""),
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
        """Send a text reply to a Lark chat; raises ``RuntimeError`` with lark-cli's reason when it fails."""
        await _run_send(send_command(text, chat_id=chat_id))

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
