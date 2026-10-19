"""The Manager in Lark direct messages and group @-mentions, over a pluggable transport.

Only allowlisted senders reach the Manager. Group messages must mention the
bot; answers and status lines reply to that message, while task notices go
to the group and mention their requester. Gate posts preserve external ids
and reply references for routing and idempotent retries.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
from ..task_notice import Notice, NotifyTarget, TargetKind
from .allowlist import SenderAllowlist
from .base import ChannelAdapter, DeliveryFailure, DeliveryReceipt, PostReceipt
from .chat_history import record_reply
from .lark_cli import MESSAGE_GONE_CODES, RECEIVE_EVENT, LarkCliError, mention
from .lark_transport import CliTransport, LarkTransport
from .status_line import Reactions, StatusKind, StatusLines, status_kind

logger = logging.getLogger(__name__)

# Told the open_id of a sender the allowlist refused and the group chat they wrote in (None for a direct
# message); never what they wrote.
RejectedListener = Callable[[str, str | None], Awaitable[None]]

# How long finding a group sender's name may hold up their message; then the Manager gets the open_id.
_NAME_LOOKUP_SECONDS = 5.0


def _check_lark_cli_available() -> bool:
    """Check if lark-cli is available for event collection."""
    import shutil
    return bool(shutil.which("lark-cli"))


class LarkChannelAdapter(ChannelAdapter):
    """Bridge a Lark transport and the agent message bus."""

    _RESTART_SECONDS = 10.0

    def __init__(
        self,
        *,
        allowlist: SenderAllowlist,
        profile: str = "",
        runtime_root: Path | None = None,
        on_rejected: RejectedListener | None = None,
        bot_open_id: str = "",
        transport: LarkTransport | None = None,
        channel_type: str = "lark",
    ) -> None:
        self._channel_type = channel_type
        self._transport = transport if transport is not None else CliTransport(profile)
        self._allowlist = allowlist  # open_ids
        self._profile = profile  # lark-cli's --profile; "" for its active one
        # The bot's own open_id: a group message is handled only when it @-mentions this; none, none is.
        self._bot_open_id = bot_open_id
        # One status message per message answered, edited in place (``status_line``), keyed by correlation id:
        # it goes where the answer goes (``_answer``).
        self._status = StatusLines(channel=self.channel_id, send=self._send_status, edit=self._edit_status)
        self._reactions = Reactions(channel=self.channel_id, react=self._react)
        self._on_rejected = on_rejected
        self._runtime_root = runtime_root or Path.cwd()
        self._bus: MessageBus | None = None
        self._address = channel_address(self.channel_type, self.channel_id)
        self._outbound_task: asyncio.Task[Any] | None = None
        self._collector_task: asyncio.Task[Any] | None = None
        self._running = False
        # correlation_id → lark chat_id for reply routing
        self._correlation_chat: dict[str, str] = {}
        # correlation_id → the group message it answers: its answer and status message reply to it
        self._group_message: dict[str, str] = {}

    @property
    def channel_type(self) -> str:
        return self._channel_type

    @property
    def channel_id(self) -> str:
        return f"channel-{self.channel_type}"

    async def start(self, bus: MessageBus) -> None:
        if not self._allowlist.ids:
            # The Manager runs any command it is asked to: nobody may reach it unlisted.
            logger.error("%s not started: its sender allowlist is empty", self.channel_id)
            return

        if not self._transport.available():
            logger.warning("%s not started: its transport is unavailable", self.channel_id)
            return

        self._bus = bus
        self._running = True
        self._outbound_task = asyncio.create_task(self._outbound_loop())
        self._collector_task = asyncio.create_task(self._event_collector_loop())
        logger.info("%s adapter started", self.channel_id)

    async def stop(self) -> None:
        self._running = False
        for task in (self._outbound_task, self._collector_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._reactions.stop()
        await self._transport.aclose()
        logger.info("%s adapter stopped", self.channel_id)

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Show a phase on the message's status line, send task progress or a task's end notice as a message of
        its own, or send the answer to the chat the message came from and close its status line."""
        if envelope.message_type is MessageType.CHAT_NOTICE:
            # A task's end, to who it recorded, in order behind its progress.
            notice = Notice.from_payload(envelope.payload)
            if notice is None:
                return DeliveryReceipt(channel=self.channel_type, success=False, error="a notice without a Lark target")
            return await self._send_notice(notice.target, notice.text)
        corr_id = envelope.correlation_id
        chat_id = self._correlation_chat.get(corr_id)
        if chat_id is None:
            return DeliveryReceipt(
                channel=self.channel_type,
                success=False,
                error=f"no chat_id for correlation {corr_id}",
                failure=DeliveryFailure.NO_CHAT,
            )

        if envelope.message_type is MessageType.STATUS_UPDATE:
            match status_kind(envelope.payload):
                case StatusKind.PHASE:
                    await self._status.update(corr_id, corr_id, envelope.payload)
                case StatusKind.PROGRESS:
                    # Task progress leaves the status line alone.
                    return await self._send_message(chat_id, str(envelope.payload["text"]))
            return DeliveryReceipt(channel=self.channel_type, success=True)

        text = self._format_reply(envelope)
        if not text:
            return DeliveryReceipt(channel=self.channel_type, success=True)

        if corr_id in self._group_message:
            receipt = await self._send_reply(corr_id, text)
        else:
            receipt = await self._send_message(chat_id, text)
        if not envelope.payload.get("autonomous"):
            await self._status.finish(corr_id, error=envelope.payload.get("error"))
        return receipt

    async def _answer(self, correlation_id: str, *, text: str | None = None,
                      markdown: str | None = None) -> str | None:
        """Answer the triggering group message, or send to its direct chat."""
        message_id = self._group_message.get(correlation_id)
        if message_id is not None:
            return await self._transport.reply(message_id, text=text, markdown=markdown)
        return await self._transport.send(chat_id=self._correlation_chat[correlation_id], text=text, markdown=markdown)

    async def _send_reply(self, correlation_id: str, text: str) -> DeliveryReceipt:
        """Answer a group message with a Markdown reply to it."""
        try:
            await self._answer(correlation_id, markdown=text)
        except LarkCliError as exc:
            logger.warning("%s: a reply in a group chat failed: %s", self.channel_id, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))
        return DeliveryReceipt(channel=self.channel_type, success=True)

    async def _send_target(self, target: NotifyTarget, text: str, *,
                           idempotency_key: str | None = None) -> str | None:
        """Send Markdown to a user or to a group mentioning the requester."""
        if target.kind is TargetKind.USER:
            return await self._transport.send(user_id=target.id, markdown=text, idempotency_key=idempotency_key)
        body = f"{mention(target.mention)} {text}" if target.mention else text
        return await self._transport.send(chat_id=target.id, markdown=body, idempotency_key=idempotency_key)

    async def _send_notice(self, target: NotifyTarget, text: str) -> DeliveryReceipt:
        """Send a task's end notice as Markdown, like an answer: to a user in a direct message, or to a group chat
        @-mentioning the requester."""
        try:
            await self._send_target(target, text)
        except LarkCliError as exc:
            logger.warning("%s: a task's end notice to %s failed: %s", self.channel_id, target.id, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))
        return DeliveryReceipt(channel=self.channel_type, success=True)

    async def _send_message(self, chat_id: str, text: str) -> DeliveryReceipt:
        try:
            await self._send_lark_message(chat_id, text)
            return DeliveryReceipt(channel=self.channel_type, success=True)
        except Exception as exc:
            logger.warning("%s delivery failed: %s", self.channel_id, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))

    async def _send_status(self, correlation_id: str, text: str) -> str | None:
        return await self._answer(correlation_id, text=text)

    async def _edit_status(self, chat_id: str, message_id: str, text: str) -> None:
        await self._transport.edit(message_id, text=text)

    async def _react(self, chat_id: str, message_id: str) -> None:
        await self._transport.react(message_id)

    async def notify(self, chat: str, text: str) -> DeliveryReceipt:
        """Send ``text`` as the bot to Lark user ``chat`` (an open_id)."""
        try:
            await self._transport.send(user_id=chat, text=text)
        except LarkCliError as exc:
            logger.warning("%s notification to %s failed: %s", self.channel_id, chat, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))
        return DeliveryReceipt(channel=self.channel_type, success=True)

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        """Send ``text`` as Markdown: a reply to the message ``reply_to`` (``om_...``) when given, which @-mentions
        nobody; else to ``target`` like an end notice (a user, or a group @-mentioning the requester). Lark sends one
        message per ``idempotency_key`` within an hour, so a post retried after a lost answer is not sent twice."""
        try:
            if reply_to:
                message_id = await self._transport.reply(reply_to, markdown=text, idempotency_key=idempotency_key)
            else:
                message_id = await self._send_target(target, text, idempotency_key=idempotency_key)
        except LarkCliError as exc:
            logger.warning("%s: a post to %s failed: %s", self.channel_id, target.id, exc)
            return PostReceipt(False, error=str(exc),
                               parent_gone=bool(reply_to) and exc.code in MESSAGE_GONE_CODES)
        if message_id is None:
            problem = ("lark-cli" if isinstance(self._transport, CliTransport) else self.channel_type) + " did not say which message it sent"
            logger.warning("%s: a post to %s: %s", self.channel_id, target.id, problem)
            return PostReceipt(False, error=problem)
        return PostReceipt(True, external_ids=(message_id,))

    def healthcheck(self) -> dict[str, Any]:
        info = {
            "channel": self.channel_type,
            "healthy": self._running,
            "active_conversations": len(self._correlation_chat),
        }

        if isinstance(self._transport, CliTransport):
            info["lark_cli_available"] = self._transport.available()
        else:
            info["transport"] = type(self._transport).__name__
        return info

    # --- Inbound: transport events → Bus ---

    async def _event_collector_loop(self) -> None:
        """Restart each ended or broken event stream after a delay."""
        while self._running:
            try:
                await self._run_event_consumer()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("%s event collector crashed, restarting in %ss", self.channel_id, self._RESTART_SECONDS)
            else:
                if self._running:
                    logger.warning("%s event consumer stopped; starting it again in %ss", self.channel_id, self._RESTART_SECONDS)
            await asyncio.sleep(self._RESTART_SECONDS)

    async def _run_event_consumer(self) -> None:
        """Handle one event stream; a malformed event does not stop its connection."""
        events = self._transport.events()
        try:
            async for event in events:
                if not self._running:
                    break
                try:
                    await self._handle_lark_event(event)
                except Exception:
                    logger.debug("%s: skipping an invalid event", self.channel_id, exc_info=True)
        finally:
            if close := getattr(events, "aclose", None):
                await close()

    async def _handle_lark_event(self, event: dict[str, Any]) -> None:
        """Publish a received text message to the Bus. The event is lark-cli's flat one: ``chat_id``,
        ``chat_type`` (``p2p`` or ``group``), ``message_id``, ``message_type``, ``sender_id`` (the open_id),
        ``sender_type``, ``mentions`` (each ``id``, an open_id, ``key`` and ``name``), ``content``, a text
        message's text with each mention shown as ``@name``, and for a reply ``reply_to`` and ``root_id``."""
        assert self._bus is not None
        import uuid as _uuid

        if event.get("type", "") != RECEIVE_EVENT:
            return
        chat_id = event.get("chat_id", "")
        sender_open_id = event.get("sender_id")
        group = event.get("chat_type") == "group"
        if not group and event.get("chat_type") != "p2p":
            logger.info("%s: ignored a message in a %s chat (%s)", self.channel_id, event.get("chat_type") or "unknown",
                        chat_id)
            return
        bot_mentions = self._bot_mentions(event)
        if group and not bot_mentions:
            # Not addressed to the bot: nothing at all happens, not even a record of who wrote.
            logger.debug("%s: ignored a group message that does not @-mention the bot (%s)", self.channel_id, chat_id)
            return

        # Before anything else: an unlisted sender is logged, recorded (who, where and when) and never answered.
        if not self._allowlist.admits(sender_open_id):
            if self._on_rejected is not None and isinstance(sender_open_id, str) and sender_open_id:
                try:
                    await self._on_rejected(sender_open_id, chat_id if group else None)
                except Exception:
                    logger.warning("%s: could not record a rejected sender", self.channel_id, exc_info=True)
            return

        if event.get("sender_type") == "bot" or event.get("message_type", "") != "text":
            return

        content = event.get("content")
        text = content if isinstance(content, str) else ""
        if not text.strip():
            return

        text = self._without_mentions(text, bot_mentions)
        if not text.strip() and not (event.get("reply_to") or event.get("root_id")):
            return

        message_id = event.get("message_id") or ""
        self._reactions.add(chat_id, message_id)  # the bot saw it; in the background, the Manager gets it now
        correlation_id = str(_uuid.uuid4())
        self._correlation_chat[correlation_id] = chat_id
        context = {"chat_id": chat_id, "chat_type": "group" if group else "p2p", "sender_id": sender_open_id or "",
                   "message_id": message_id}
        # What the message replies to: the message it answers and the first of its thread (a gate's messages).
        for key in ("reply_to", "root_id"):
            if isinstance(ref := event.get(key), str) and ref:
                context[key] = ref
        if group:
            if message_id:
                self._group_message[correlation_id] = message_id
            context["sender_name"] = await self._sender_name(sender_open_id or "")

        envelope = MessageEnvelope.create(
            source=self._address,
            target=MANAGER_ADDRESS,
            message_type=MessageType.USER_MESSAGE,
            payload={
                "text": text,
                "language": "zh",
                "channel_context": context,
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
                # An answer is stored once it was sent, or as not delivered (the Manager names its record).
                await record_reply(self.channel_type, envelope, await self.deliver(envelope))
        except asyncio.CancelledError:
            pass

    async def _send_lark_message(self, chat_id: str, text: str) -> None:
        """Send a Markdown reply to a direct chat."""
        await self._transport.send(chat_id=chat_id, markdown=text)

    @staticmethod
    def _format_reply(envelope: MessageEnvelope) -> str:
        """The text of an answer; a status is handled by ``deliver``."""
        match envelope.message_type:
            case MessageType.AGENT_REPLY | MessageType.CLARIFICATION_REQUEST:
                return envelope.payload.get("text", "")
            case _:
                return ""

    def _bot_mentions(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """The event's mentions of the bot, by its open_id."""
        mentions = event.get("mentions")
        if not self._bot_open_id or not isinstance(mentions, list):
            return []
        return [m for m in mentions if isinstance(m, dict) and m.get("id") == self._bot_open_id]

    @staticmethod
    def _without_mentions(text: str, mentions: list[dict[str, Any]]) -> str:
        """``text`` without the bot's mentions, each once: as lark-cli shows it (``@name``), else as its placeholder
        (``@_user_1``)."""
        for found in mentions:
            shown = next((form for form in (f"@{found.get('name') or ''}", str(found.get("key") or ""))
                          if len(form) > 1 and form in text), None)
            if shown is not None:
                text = text.replace(shown, "", 1)
        return re.sub(r" {2,}", " ", text).strip()

    async def _sender_name(self, open_id: str) -> str:
        """A group sender's name, as the allowlist resolves it; their open_id when it cannot be found in time."""
        try:
            found = await asyncio.wait_for(self._transport.names([open_id]), _NAME_LOOKUP_SECONDS)
        except (TimeoutError, LarkCliError):
            found = {}
        return found.get(open_id) or open_id
