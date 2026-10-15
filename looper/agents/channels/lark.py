"""Lark (lark-cli) channel adapter: the Manager in Lark direct messages, through ``lark-cli``.

``lark-cli event consume im.message.receive_v1 --as bot`` streams the
messages the bot receives, one flat JSON object per line (``lark-cli event
schema im.message.receive_v1``); ``lark-cli im +messages-send --as bot`` posts
the replies, as Markdown, to the chat each message came from, and a
schedule's messages, as text, to a user. Every call carries the channel's
``--profile`` when it names one (``lark_cli``): the profile chooses the app and
so the bot.

Only direct messages (``chat_type`` ``p2p``) are handled: a group message is
logged and ignored, and the bot never answers in a group. Only the senders on
its allowlist (their ``open_id`` for this app) reach the Manager: anyone else
is dropped with one log line, recorded as a rejected sender (``on_rejected``)
and gets no reply, and an empty list keeps the adapter from starting.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..bus import MessageBus
from ..message import (
    MANAGER_ADDRESS,
    MessageEnvelope,
    MessageType,
    channel_address,
)
from .allowlist import SenderAllowlist
from .base import ChannelAdapter, DeliveryReceipt
from .lark_cli import RECEIVE_EVENT, LarkCliError, consume_command, run, send_command

logger = logging.getLogger(__name__)

# How much of the consumer's stderr is kept for the log line written when it stops.
_CONSUMER_STDERR_LINES = 5
# How long a consumer gets to stop after its stdin closes, and then after SIGTERM, before it is killed.
_CONSUMER_STOP_GRACE_SEC = 5.0


async def _stop_consumer(process: asyncio.subprocess.Process) -> None:
    """Stop ``lark-cli event consume`` the way it asks: close its stdin, then SIGTERM; SIGKILL only if both fail
    (it skips lark-cli's cleanup of the server-side subscription)."""
    if process.returncode is not None:
        return
    for step in ("stdin", "term"):
        try:
            if step == "stdin":
                if process.stdin is not None and not process.stdin.is_closing():
                    process.stdin.close()
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=_CONSUMER_STOP_GRACE_SEC)
            return
        except asyncio.TimeoutError:
            continue
    try:
        process.kill()
    except ProcessLookupError:
        pass

# Told the open_id of a sender the allowlist refused (never what they wrote).
RejectedListener = Callable[[str], Awaitable[None]]


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
        profile: str = "",
        runtime_root: Path | None = None,
        on_rejected: RejectedListener | None = None,
    ) -> None:
        self._allowlist = allowlist  # open_ids
        self._profile = profile  # lark-cli's --profile; "" for its active one
        self._on_rejected = on_rejected
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

        self._bus = bus
        self._running = True
        self._outbound_task = asyncio.create_task(self._outbound_loop())
        self._collector_task = asyncio.create_task(self._event_collector_loop())
        logger.info("lark channel adapter started (profile %s)", self._profile or "lark-cli's active one")

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
            await run(send_command(user_id=chat, text=text, profile=self._profile))
        except LarkCliError as exc:
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
        """Run ``lark-cli event consume`` and handle the events it prints.

        Its stdin is a pipe kept open for as long as it runs: lark-cli (1.0.97) treats stdin EOF as the signal to
        stop, so a consumer started with the server's stdin (``/dev/null`` under launchd) stops at once. Closing
        that pipe is also how it is stopped, so it can end its server-side subscription; SIGKILL is the last resort.
        What it wrote to stderr last is logged when it stops while the channel runs.
        """
        process = await asyncio.create_subprocess_exec(
            *consume_command(self._profile),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        stderr_tail: deque[str] = deque(maxlen=_CONSUMER_STDERR_LINES)

        async def _read_stderr() -> None:
            assert process.stderr is not None
            async for raw in process.stderr:
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    stderr_tail.append(text[:300])

        stderr_task = asyncio.create_task(_read_stderr())
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
            await _stop_consumer(process)
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            if self._running and stderr_tail:
                logger.warning("lark event consumer stopped (exit %s); its last output: %s",
                               process.returncode, " | ".join(stderr_tail))

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
        if event.get("chat_type") != "p2p":
            # Direct messages only: the bot never answers in a group.
            logger.info("channel-lark: ignored a message in a %s chat (%s); only direct messages are handled",
                        event.get("chat_type") or "unknown", chat_id)
            return

        # Before anything else: an unlisted sender is logged, recorded (only who and when) and never answered.
        if not self._allowlist.admits(sender_open_id):
            if self._on_rejected is not None and isinstance(sender_open_id, str) and sender_open_id:
                try:
                    await self._on_rejected(sender_open_id)
                except Exception:
                    logger.warning("channel-lark: could not record a rejected sender", exc_info=True)
            return

        if event.get("sender_type") == "bot" or event.get("message_type", "") != "text":
            return

        content = event.get("content")
        text = content if isinstance(content, str) else ""
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
        """Send a reply to a Lark chat as Markdown (a rich-text post); raises ``LarkCliError`` with lark-cli's
        reason when it fails."""
        await run(send_command(chat_id=chat_id, markdown=text, profile=self._profile))

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
