"""Lark (lark-cli) channel adapter: the Manager in Lark direct messages and group chats, through ``lark-cli``.

``lark-cli event consume im.message.receive_v1 --as bot`` streams the
messages the bot receives, one flat JSON object per line (``lark-cli event
schema im.message.receive_v1``); ``lark-cli im +messages-send --as bot`` posts
the replies, as Markdown, to the chat each message came from, and a
schedule's messages, as text, to a user. Every call carries the channel's
``--profile`` when it names one (``lark_cli``): the profile chooses the app and
so the bot.

A direct message (``chat_type`` ``p2p``) is handled as it comes. In a group
chat only a message that @-mentions the bot is: one whose ``mentions`` hold
the bot's own open_id (``bot_open_id``, from the bot's info), never a match on
names. Every other group message is ignored before anything else: no
reaction, no record, nothing reaches the Manager. The bot's mention is taken
out of the text, the Manager is told the sender's name (``sender_name``), and
the status message and the answer are replies to the message (``+messages-reply``);
a task's progress goes to the group, and its end notice too, @-mentioning
the requester. So the bot posts in a group only to answer an allowlisted
@-mention and for that task.

A user gate's messages are posted directly (``post``: to the user or the
group, or as a reply to a message, returning the id of the message sent), and
a reply's references (``reply_to``, ``root_id``) reach the Manager in
``channel_context``, so a reply to a gate's message can be routed to its gate.

Only the senders on its allowlist (their ``open_id`` for this app) reach the
Manager: anyone else is dropped with one log line, recorded as a rejected
sender (``on_rejected``: who, and in which group chat) and gets no reply, and
an empty list keeps the adapter from starting. A message that reaches the
Manager first gets the bot's 「Get」 reaction (``status_line.Reactions``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
from ..task_notice import Notice, NotifyTarget, TargetKind
from . import lark_cli
from .allowlist import SenderAllowlist
from .base import ChannelAdapter, DeliveryFailure, DeliveryReceipt, PostReceipt
from .chat_history import record_reply
from .lark_cli import (
    MESSAGE_GONE_CODES, RECEIVE_EVENT, LarkCliError, consume_command, edit_command, mention, reaction_command, reply_command, run,
    run_json, send_command, sent_message_id,
)
from .status_line import Reactions, StatusKind, StatusLines, status_kind

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
        bot_open_id: str = "",
    ) -> None:
        self._allowlist = allowlist  # open_ids
        self._profile = profile  # lark-cli's --profile; "" for its active one
        # The bot's own open_id: a group message is handled only when it @-mentions this; none, none is.
        self._bot_open_id = bot_open_id
        # One status message per message answered, edited in place (``status_line``), keyed by correlation id:
        # it goes where the answer goes (``_answer_command``).
        self._status = StatusLines(channel="channel-lark", send=self._send_status, edit=self._edit_status)
        self._reactions = Reactions(channel="channel-lark", react=self._react)
        self._on_rejected = on_rejected
        self._runtime_root = runtime_root or Path.cwd()
        self._bus: MessageBus | None = None
        self._address = channel_address("lark", "channel-lark")
        self._outbound_task: asyncio.Task[Any] | None = None
        self._collector_task: asyncio.Task[Any] | None = None
        self._running = False
        # correlation_id → lark chat_id for reply routing
        self._correlation_chat: dict[str, str] = {}
        # correlation_id → the group message it answers: its answer and status message reply to it
        self._group_message: dict[str, str] = {}

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
        await self._reactions.stop()
        logger.info("lark channel adapter stopped")

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Show a phase on the message's status line, send task progress or a task's end notice as a message of
        its own, or send the answer to the chat the message came from and close its status line."""
        if envelope.message_type is MessageType.CHAT_NOTICE:
            # A task's end, to who it recorded, in order behind its progress.
            notice = Notice.from_payload(envelope.payload)
            if notice is None:
                return DeliveryReceipt(channel="lark", success=False, error="a notice without a Lark target")
            return await self._send_notice(notice.target, notice.text)
        corr_id = envelope.correlation_id
        chat_id = self._correlation_chat.get(corr_id)
        if chat_id is None:
            return DeliveryReceipt(
                channel="lark",
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
            return DeliveryReceipt(channel="lark", success=True)

        text = self._format_reply(envelope)
        if not text:
            return DeliveryReceipt(channel="lark", success=True)

        if corr_id in self._group_message:
            receipt = await self._send_reply(corr_id, text)
        else:
            receipt = await self._send_message(chat_id, text)
        if not envelope.payload.get("autonomous"):
            await self._status.finish(corr_id, error=envelope.payload.get("error"))
        return receipt

    def _answer_command(self, correlation_id: str, *, text: str | None = None,
                        markdown: str | None = None) -> list[str]:
        """The argv that answers the message ``correlation_id``: a reply to it in a group, else a message to its
        direct chat."""
        message_id = self._group_message.get(correlation_id)
        if message_id is not None:
            return reply_command(message_id, text=text, markdown=markdown, profile=self._profile)
        return send_command(chat_id=self._correlation_chat[correlation_id], text=text, markdown=markdown,
                            profile=self._profile)

    async def _send_reply(self, correlation_id: str, text: str) -> DeliveryReceipt:
        """Answer a group message with a Markdown reply to it."""
        try:
            await run(self._answer_command(correlation_id, markdown=text))
        except LarkCliError as exc:
            logger.warning("lark: a reply in a group chat failed: %s", exc)
            return DeliveryReceipt(channel="lark", success=False, error=str(exc))
        return DeliveryReceipt(channel="lark", success=True)

    def _target_command(self, target: NotifyTarget, text: str, *, idempotency_key: str | None = None) -> list[str]:
        """The argv that sends ``text`` as Markdown to ``target``: to a user in a direct message, or to a group chat
        @-mentioning the requester."""
        if target.kind is TargetKind.USER:
            return send_command(user_id=target.id, markdown=text, profile=self._profile,
                                idempotency_key=idempotency_key)
        body = f"{mention(target.mention)} {text}" if target.mention else text
        return send_command(chat_id=target.id, markdown=body, profile=self._profile, idempotency_key=idempotency_key)

    async def _send_notice(self, target: NotifyTarget, text: str) -> DeliveryReceipt:
        """Send a task's end notice as Markdown, like an answer: to a user in a direct message, or to a group chat
        @-mentioning the requester."""
        try:
            await run(self._target_command(target, text))
        except LarkCliError as exc:
            logger.warning("lark: a task's end notice to %s failed: %s", target.id, exc)
            return DeliveryReceipt(channel="lark", success=False, error=str(exc))
        return DeliveryReceipt(channel="lark", success=True)

    async def _send_message(self, chat_id: str, text: str) -> DeliveryReceipt:
        try:
            await self._send_lark_message(chat_id, text)
            return DeliveryReceipt(channel="lark", success=True)
        except Exception as exc:
            logger.warning("lark delivery failed: %s", exc)
            return DeliveryReceipt(channel="lark", success=False, error=str(exc))

    async def _send_status(self, correlation_id: str, text: str) -> str | None:
        return sent_message_id(await run_json(self._answer_command(correlation_id, text=text)))

    async def _edit_status(self, chat_id: str, message_id: str, text: str) -> None:
        await run(edit_command(message_id, text=text, profile=self._profile))

    async def _react(self, chat_id: str, message_id: str) -> None:
        await run(reaction_command(message_id, profile=self._profile))

    async def notify(self, chat: str, text: str) -> DeliveryReceipt:
        """Send ``text`` as the bot to Lark user ``chat`` (an open_id)."""
        try:
            await run(send_command(user_id=chat, text=text, profile=self._profile))
        except LarkCliError as exc:
            logger.warning("lark notification to %s failed: %s", chat, exc)
            return DeliveryReceipt(channel="lark", success=False, error=str(exc))
        return DeliveryReceipt(channel="lark", success=True)

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        """Send ``text`` as Markdown: a reply to the message ``reply_to`` (``om_...``) when given, which @-mentions
        nobody; else to ``target`` like an end notice (a user, or a group @-mentioning the requester). Lark sends one
        message per ``idempotency_key`` within an hour, so a post retried after a lost answer is not sent twice."""
        if reply_to:
            argv = reply_command(reply_to, markdown=text, profile=self._profile, idempotency_key=idempotency_key)
        else:
            argv = self._target_command(target, text, idempotency_key=idempotency_key)
        try:
            message_id = sent_message_id(await run_json(argv))
        except LarkCliError as exc:
            logger.warning("lark: a post to %s failed: %s", target.id, exc)
            return PostReceipt(False, error=str(exc),
                               parent_gone=bool(reply_to) and exc.code in MESSAGE_GONE_CODES)
        if message_id is None:
            logger.warning("lark: a post to %s: lark-cli did not say which message it sent", target.id)
            return PostReceipt(False, error="lark-cli did not say which message it sent")
        return PostReceipt(True, external_ids=(message_id,))

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
            logger.info("channel-lark: ignored a message in a %s chat (%s)", event.get("chat_type") or "unknown",
                        chat_id)
            return
        bot_mentions = self._bot_mentions(event)
        if group and not bot_mentions:
            # Not addressed to the bot: nothing at all happens, not even a record of who wrote.
            logger.debug("channel-lark: ignored a group message that does not @-mention the bot (%s)", chat_id)
            return

        # Before anything else: an unlisted sender is logged, recorded (who, where and when) and never answered.
        if not self._allowlist.admits(sender_open_id):
            if self._on_rejected is not None and isinstance(sender_open_id, str) and sender_open_id:
                try:
                    await self._on_rejected(sender_open_id, chat_id if group else None)
                except Exception:
                    logger.warning("channel-lark: could not record a rejected sender", exc_info=True)
            return

        if event.get("sender_type") == "bot" or event.get("message_type", "") != "text":
            return

        content = event.get("content")
        text = content if isinstance(content, str) else ""
        if not text.strip():
            return

        text = self._without_mentions(text, bot_mentions)
        if not text.strip():
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
        """Send a reply to a Lark chat as Markdown (a rich-text post); raises ``LarkCliError`` with lark-cli's
        reason when it fails."""
        await run(send_command(chat_id=chat_id, markdown=text, profile=self._profile))

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
            found = await asyncio.wait_for(lark_cli.names([open_id], self._profile or None), _NAME_LOOKUP_SECONDS)
        except (TimeoutError, LarkCliError):
            found = {}
        return found.get(open_id) or open_id
