"""The Manager in Lark direct messages and group @-mentions, over a pluggable transport.

Senders reach the Manager through an allowlist or an explicitly open chat binding. Group messages must mention the
bot; answers and status lines reply to that message, while task notices go
to the group and mention their requester. Gate posts preserve external ids
and reply references for routing and idempotent retries.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from random import uniform as _reconnect_jitter
from time import monotonic, time
from typing import Any, Literal

from ..bus import MessageBus
from .. import chat_attachments as files
from ..chat_attachments import Attachment, AttachmentKind, AttachmentState, TooLarge
from ..message import (
    MessageEnvelope,
    MessageType,
    builtin_channel_id,
    channel_address,
    channel_thread_id,
    manager_address,
    parse_channel_id,
)
from ..task_notice import Notice, NotifyTarget, TargetKind
from .allowlist import SenderAllowlist
from .base import ChannelAdapter, DeliveryFailure, DeliveryReceipt, PostReceipt
from .chat_history import record_reply
from .chat_routing import ChatResolver, RouteAction, RouteDecision, SenderPolicy, observe_chat, log_route
from . import lark_content, lark_quote
from .lark_cli import MESSAGE_GONE_CODES, RECEIVE_EVENT, LarkCliError, mention
from .lark_quote import Quote, QuoteKind
from .lark_transport import CliTransport, LarkTransport
from .lark_inbox import LarkInbox, history_event
from .status_line import Reactions, StatusKind, StatusLines, status_kind

logger = logging.getLogger(__name__)

# Told the open_id of a sender the allowlist refused and the group chat they wrote in (None for a direct
# message); never what they wrote.
RejectedListener = Callable[[str, str | None], Awaitable[None]]

# How long finding a group sender's name may hold up their message; then the Manager gets the open_id.
_NAME_LOOKUP_SECONDS = 5.0
# How long reading what a message quotes may hold it up, names included; then it goes on with what was read. A
# long merged forward's raw read fails after about 5 s and its rendered read then takes about 6 s.
_QUOTE_SECONDS = 15.0
_DOWNLOAD_MESSAGE_SECONDS = 60.0
# Messages read in the background at once (what they quote, their downloads), across chats.
_DELIVERY_CONCURRENCY = 4
# Unpublished messages, in one chat and in all: past either, a message's quotes are not read ("busy").
_CHAT_PENDING_MAX = 10
_PENDING_MAX = 100
# The waits between a background message's tries to publish it, or to record it; then it is given up (held).
_DELIVERY_RETRY_SECONDS = (0.5, 1.0, 2.0, 4.0)
# Group messages in a thread, whose answers go in the thread: the latest ones kept.
_THREAD_MESSAGES_KEPT = 1_000
_CATCHUP_MAX_AGE = 15 * 60
_CATCHUP_OVERLAP = 10
_CATCHUP_MAX_CHATS = 100
_CATCHUP_MAX_PAGES = 5
_CATCHUP_ROOT_MAX_AGE = 24 * 60 * 60
_CATCHUP_MAX_THREADS = 50
_CATCHUP_THREAD_CONCURRENCY = 4
_HEARTBEAT_SECONDS = 5
_BOT_POLL_SECONDS = 30
_BOT_SLOW_POLL_SECONDS = 5 * 60
_BOT_POLL_LOG_SECONDS = 60 * 60
_BOT_TURNS_PER_HOUR = 10
_BOT_WINDOW_SECONDS = 60 * 60


def _history_messages(messages: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for message in messages:
        yield message
        yield from _history_messages(message.get("thread_replies") or [])


@dataclass(frozen=True)
class _HistoryTime:
    at: float
    minute: bool = False

    def before(self, cutoff: float) -> bool:
        # FormatTime's local minute denotes [at, at + 60), not an exact :00.
        return self.at + 60 <= cutoff if self.minute else self.at < cutoff


def _history_created(message: dict[str, Any]) -> _HistoryTime:
    raw_time = message["create_time"]
    try:
        created = float(raw_time)
        if created > 100_000_000_000:
            created /= 1000
    except ValueError:
        created = datetime.fromisoformat(raw_time.replace("Z", "+00:00")).timestamp()
    if not math.isfinite(created):
        raise ValueError("history creation time is not finite")
    return _HistoryTime(created, isinstance(raw_time, str) and bool(
        re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", raw_time)))


def _not_received(attachments: list[Attachment]) -> str:
    items = []
    for item in attachments:
        if item.state is AttachmentState.SAVED:
            continue
        label = f"图片 {item.index} " if item.kind is AttachmentKind.IMAGE else f"文件 {item.index}（{item.name}）"
        reason = {
            AttachmentState.TOO_LARGE: "超过 20 MB",
            AttachmentState.NOT_ACCEPTED: "不是支持的类型（图片、文本、PDF、Office 文档）",
            AttachmentState.OVER_LIMIT: "超出一条消息 10 个附件的上限",
        }.get(item.state, "下载失败")
        if "im:message:readonly" in (item.reason or ""):
            reason += "（缺少权限 im:message:readonly）"
        items.append(label + reason)
    return f"有 {len(items)} 个附件没有收下：" + "；".join(items) + "。其余内容已交给 Manager。"


class _DeliveryState(str, Enum):
    PENDING = "pending"  # being read, or waiting for its chat's earlier messages
    PUBLISHED = "published"  # on the bus, its receipt not written yet
    RECORDED = "recorded"  # its receipt written: done
    HELD = "held"  # given up: not recorded, so it holds the restart boundary and its chat's later messages


@dataclass
class _Delivery:
    """An admitted message on its way to the Manager: what is read for it (``targets``, its resources, its sender's
    name) and what its envelope carries. ``withdraw`` publishes it at once, as withdrawn, with nothing more read (a
    ``/stop``); ``busy`` leaves its quotes unread (too many messages are waiting)."""

    event: dict[str, Any]
    chat_id: str
    group: bool
    message_id: str
    text: str
    inbound: lark_content.Inbound
    manager_id: str
    metadata: dict[str, Any]
    sender: str
    targets: list[tuple[QuoteKind, str]]
    withdraw: asyncio.Event = field(default_factory=asyncio.Event)
    busy: bool = False
    state: _DeliveryState = _DeliveryState.PENDING
    previous: _Delivery | None = None
    task: asyncio.Task[None] | None = None


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
        bot_app_id: str = "",
        transport: LarkTransport | None = None,
        channel_type: str = "lark",
        backoff: bool = False,
        channel_id: str | None = None,
        manager_id: str = "manager",
        manager_running: Callable[[str], bool] | None = None,
        resolve_chat: ChatResolver | None = None,
        stop_chat: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._channel_type = channel_type
        self._channel_id = channel_id or builtin_channel_id(channel_type)
        parsed = parse_channel_id(self._channel_id)
        if parsed is None or parsed.channel_type != channel_type:
            raise ValueError(f"not a {channel_type} channel id: {self._channel_id!r}")
        self._manager_id = manager_id
        self._manager_running = manager_running
        self._resolve_chat = resolve_chat
        self._stop_chat = stop_chat
        self._backoff = backoff
        self._transport = transport if transport is not None else CliTransport(profile)
        self._allowlist = allowlist  # open_ids
        self._profile = profile  # lark-cli's --profile; "" for its active one
        # The bot's own open_id: a group message is handled only when it @-mentions this; none, none is.
        self._bot_open_id = bot_open_id
        self._bot_app_id = bot_app_id
        self._bot_turns: dict[str, deque[float]] = {}
        self._bot_refusals: dict[tuple[str, str], float] = {}
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
        self._catchup_task: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._bot_poll_task: asyncio.Task[Any] | None = None
        self._bot_poll_logged_at: float | None = None
        self._inbox: LarkInbox | None = None
        self._ingress_lock = asyncio.Lock()
        # Messages read in the background, by id, until their receipts are written (a held one stays: it keeps the
        # restart boundary, a catch-up window and the bot poll where they are); each chat's last one, which the next
        # waits for, so a chat reaches the Manager in order; the chats whose messages are held.
        self._unsettled: dict[str, _Delivery] = {}
        self._chat_tail: dict[str, _Delivery] = {}
        self._held_chats: set[str] = set()
        self._delivery_slots = asyncio.Semaphore(_DELIVERY_CONCURRENCY)
        self._running = False
        # correlation_id → lark chat_id for reply routing
        self._correlation_chat: dict[str, str] = {}
        # correlation_id → the group message it answers: its answer and status message reply to it
        self._group_message: dict[str, str] = {}
        self._thread_messages: dict[str, None] = {}  # the latest _THREAD_MESSAGES_KEPT, oldest first

    @property
    def channel_type(self) -> str:
        return self._channel_type

    @property
    def channel_id(self) -> str:
        return self._channel_id

    async def start(self, bus: MessageBus) -> None:
        if not self._allowlist.ids:
            # The Manager runs any command it is asked to: nobody may reach it unlisted.
            logger.error("%s not started: its sender allowlist is empty", self.channel_id)
            return

        if not self._transport.available():
            logger.warning("%s not started: its transport is unavailable", self.channel_id)
            return

        if self.channel_type == "lark":
            self._inbox = await LarkInbox.open(bus.runtime_root / ".local" / "lark" / self.channel_id / "inbox.sqlite3")
            if isinstance(self._transport, CliTransport):
                self._transport.on_thread_activity = self._remember_thread
            boundary = self._inbox.boundary
            # Discovery itself can fail or be cancelled while live checkpoints advance.
            if boundary is not None and time() - boundary <= _CATCHUP_MAX_AGE:
                await self._inbox.defer_discovery(boundary - _CATCHUP_OVERLAP, time())
            # Keep the live boundary until the consumer is healthy.
            await self._inbox.checkpoint(boundary if boundary is not None else time())
        else:
            boundary = None
            if self._resolve_chat is not None and any(
                    (route := self._resolve_chat(self.channel_id, chat_id)).explicit
                    and (route.sender_policy == SenderPolicy.CHAT
                         or any(sender.startswith("cli_") for sender in route.senders | self._allowlist.ids))
                    for chat_id in await self._bound_chats()):
                logger.warning(
                    "%s: messages sent by other bots are not received on this channel type unless the app "
                    "has Lark's permission to receive bot messages; alternatively use the lark-cli channel's "
                    "polling", self.channel_id)
        self._bus = bus
        self._running = True
        self._outbound_task = asyncio.create_task(self._outbound_loop())
        self._collector_task = asyncio.create_task(self._event_collector_loop())
        if self._inbox is not None:
            self._catchup_task = asyncio.create_task(self._catch_up(boundary))
            self._heartbeat_task = asyncio.create_task(self._heartbeat())
            self._bot_poll_task = asyncio.create_task(self._bot_poll_loop())
        logger.info("%s adapter started", self.channel_id)

    async def stop(self) -> None:
        # Cancellation during admission/download must not move the recovery boundary past that message.
        ready = getattr(self._transport, "consumer_started", None)
        clean = (not self._ingress_lock.locked() and not self._unsettled
                 and ready is not None and ready.is_set())
        stopped_at = time()
        self._running = False
        for task in (self._bot_poll_task, self._heartbeat_task, self._catchup_task, self._outbound_task, self._collector_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        deliveries = [delivery.task for delivery in self._unsettled.values() if delivery.task is not None]
        for task in deliveries:
            task.cancel()
        await asyncio.gather(*deliveries, return_exceptions=True)
        await self._reactions.stop()
        if isinstance(self._transport, CliTransport):
            self._transport.on_thread_activity = None
        await self._transport.aclose()
        if self._inbox is not None:
            if clean:
                await self._inbox.checkpoint(stopped_at)
            await self._inbox.close()
            self._inbox = None
        logger.info("%s adapter stopped", self.channel_id)

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Show a phase on the message's status line, send task progress or a task's end notice as a message of
        its own, or send the answer to the chat the message came from and close its status line."""
        envelope = replace(envelope, payload={
            key: files.redact(value, self._runtime_root, paths=envelope.metadata.get("attachment_paths", ()))
            if isinstance(value, str) else value for key, value in envelope.payload.items()
        })
        if envelope.message_type is MessageType.CHAT_NOTICE:
            # A task's end, to who it recorded, in order behind its progress.
            notice = Notice.from_payload(envelope.payload)
            if notice is None:
                return DeliveryReceipt(channel=self.channel_type, success=False, error="a notice without a Lark target")
            return await self._send_notice(notice.target, notice.text)
        corr_id = envelope.correlation_id
        chat_id = self._correlation_chat.get(corr_id)
        if chat_id is None and envelope.target == self._address:
            # Restore both destinations: a group's answer/status must reply to its original message.
            route = envelope.metadata.get("reply_route")
            if isinstance(route, dict) and isinstance(route.get("chat_id"), str) and route["chat_id"]:
                if (envelope.message_type is MessageType.STATUS_UPDATE
                        and status_kind(envelope.payload) is StatusKind.PROGRESS
                        and isinstance(route.get("reply_to"), str) and route["reply_to"]):
                    # A recovered task report answers its original message without changing live routing.
                    try:
                        await self._reply(route["reply_to"], markdown=str(envelope.payload["text"]))
                    except LarkCliError as exc:
                        logger.warning("%s: a recovered task progress reply failed: %s", self.channel_id, exc)
                        if exc.code in MESSAGE_GONE_CODES:
                            return await self._send_message(route["chat_id"], str(envelope.payload["text"]))
                        return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))
                    return DeliveryReceipt(channel=self.channel_type, success=True)
                chat_id = route["chat_id"]
                self._correlation_chat[corr_id] = chat_id
                if isinstance(route.get("reply_to"), str) and route["reply_to"]:
                    self._group_message[corr_id] = route["reply_to"]
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
            await self._status.finish(corr_id, error=envelope.payload.get("error"),
                                      stopped=envelope.payload.get("outcome") == "stopped")
        return receipt

    async def _reply(self, message_id: str, **kwargs: Any) -> str | None:
        if message_id in self._thread_messages:
            kwargs["reply_in_thread"] = True
        return await self._transport.reply(message_id, **kwargs)

    async def _answer(self, correlation_id: str, *, text: str | None = None,
                      markdown: str | None = None, return_id: bool = False) -> str | None:
        """Answer the triggering group message, or send to its direct chat."""
        message_id = self._group_message.get(correlation_id)
        if message_id is not None:
            return await self._reply(message_id, text=text, markdown=markdown, return_id=return_id)
        return await self._transport.send(chat_id=self._correlation_chat[correlation_id], text=text, markdown=markdown,
                                          return_id=return_id)

    async def _send_reply(self, correlation_id: str, text: str) -> DeliveryReceipt:
        """Answer a group message with a Markdown reply to it."""
        try:
            await self._answer(correlation_id, markdown=text)
        except LarkCliError as exc:
            logger.warning("%s: a reply in a group chat failed: %s", self.channel_id, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))
        return DeliveryReceipt(channel=self.channel_type, success=True)

    async def _send_target(self, target: NotifyTarget, text: str, *,
                           idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        """Send Markdown to a user or to a group mentioning the requester."""
        text = files.redact(text, self._runtime_root)
        if target.kind is TargetKind.USER:
            return await self._transport.send(user_id=target.id, markdown=text, idempotency_key=idempotency_key,
                                              return_id=return_id)
        body = f"{mention(target.mention)} {text}" if target.mention else text
        return await self._transport.send(chat_id=target.id, markdown=body, idempotency_key=idempotency_key,
                                          return_id=return_id)

    async def _send_notice(self, target: NotifyTarget, text: str) -> DeliveryReceipt:
        """Send a task's end notice as Markdown, like an answer: to a user in a direct message, or to a group chat
        @-mentioning the requester."""
        try:
            # Rich-text posts render Markdown; keep the durable notice unchanged.
            rendered = re.sub(
                r'(^## Process record\n\n)((?:- (?:Roles|Reviews|Verification|Escalations): .*\n?)+)',
                lambda block: block[1] + re.sub(r'([\\`*_~\[\]<>])', r'\\\1', block[2]),
                text, flags=re.MULTILINE)
            await self._send_target(target, rendered)
        except LarkCliError as exc:
            logger.warning("%s: a task's end notice to %s failed: %s", self.channel_id, target.id, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))
        return DeliveryReceipt(channel=self.channel_type, success=True)

    async def _turned_off(self, chat_id: str, group_message: str | None, *, manager_id: str,
                          explicit: bool = False, stop: bool = False) -> bool:
        """Whether this bot's Manager is turned off; then the sender is told so (under their message in a group)
        and the message goes nowhere."""
        if manager_id == "manager" and not explicit:
            return False
        try:
            running = self._manager_running(manager_id) if self._manager_running is not None else not explicit
        except Exception:
            logger.warning("%s: cannot tell whether %s is running", self.channel_id, manager_id, exc_info=True)
            if not explicit:
                return False
            running = False
        if running:
            return False
        from ..tiers.manager import TURNED_OFF_REPLY

        text = TURNED_OFF_REPLY
        if stop and self._stop_chat is not None:
            from ..managers import chat_stop_reply

            text = chat_stop_reply(await self._stop_chat(self.channel_id, chat_id, via=self.channel_type))
        outcome = "answered"
        try:
            if group_message:
                await self._reply(group_message, text=text)
            else:
                await self._transport.send(chat_id=chat_id, text=text)
        except Exception as exc:
            outcome = "answer_failed"
            logger.warning("%s: could not tell chat %s that %s is turned off: %s", self.channel_id, chat_id,
                           manager_id, exc)
        log_route(logger, self.channel_id, chat_id, RouteDecision(RouteAction.ADMIT, manager_id, explicit),
                  reason="disabled_target", outcome=outcome)
        return True

    async def _send_message(self, chat_id: str, text: str) -> DeliveryReceipt:
        try:
            await self._send_lark_message(chat_id, text)
            return DeliveryReceipt(channel=self.channel_type, success=True)
        except Exception as exc:
            logger.warning("%s delivery failed: %s", self.channel_id, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))

    async def _send_status(self, correlation_id: str, text: str) -> str | None:
        return await self._answer(correlation_id, text=text, return_id=True)

    async def _edit_status(self, chat_id: str, message_id: str, text: str) -> None:
        await self._transport.edit(message_id, text=text)

    async def _react(self, chat_id: str, message_id: str) -> None:
        await self._transport.react(message_id)

    async def notify(self, chat: str, text: str) -> DeliveryReceipt:
        """Send ``text`` as the bot to a Lark user (open_id) or native chat (chat_id)."""
        text = files.redact(text, self._runtime_root)
        try:
            # Lark prefixes chat IDs with oc_ and user open IDs with ou_.
            if chat.startswith("oc_"):
                destination = {"chat_id": chat}
            elif chat.startswith("ou_"):
                destination = {"user_id": chat}
            else:
                raise LarkCliError("notification target must be a Lark chat ID (oc_) or user open ID (ou_)")
            await self._transport.send(**destination, text=text)
        except LarkCliError as exc:
            logger.warning("%s notification to %s failed: %s", self.channel_id, chat, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc))
        return DeliveryReceipt(channel=self.channel_type, success=True)

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        """Send ``text`` as Markdown: a reply to the message ``reply_to`` (``om_...``) when given, which @-mentions
        nobody; else to ``target`` like an end notice (a user, or a group @-mentioning the requester). Lark sends one
        message per ``idempotency_key`` within an hour, so a post retried after a lost answer is not sent twice."""
        text = files.redact(text, self._runtime_root)
        try:
            if reply_to:
                message_id = await self._reply(reply_to, markdown=text, idempotency_key=idempotency_key,
                                                         return_id=True)
            else:
                message_id = await self._send_target(target, text, idempotency_key=idempotency_key, return_id=True)
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

    async def _catchup_chats(self) -> list[str]:
        from ...db.chat_bindings import catchup_chats

        return await catchup_chats(self.channel_id, _CATCHUP_MAX_CHATS)

    async def _history_pages(self, fetch: Callable[..., Awaitable[dict[str, Any]]],
                             container_id: str, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        token = ""
        for _ in range(_CATCHUP_MAX_PAGES):
            page = await fetch(container_id, page_token=token, **kwargs)
            yield page
            if not page.get("has_more"):
                return
            next_token = page.get("page_token")
            if not next_token or next_token == token:
                raise ValueError("history pagination did not advance")
            token = next_token
        raise ValueError("catch-up page limit reached")

    async def _catchup_messages(self, page: dict[str, Any], chat_id: str, *, start: float, end: float,
                                stats: dict[str, int], bots_only: bool = False) -> None:
        for message in _history_messages(page["messages"][:50]):
            stats["messages_found"] += 1
            try:
                created = _history_created(message)
                # Observe all app senders, even old, unmentioned or unlisted messages.
                sender = message.get("sender") or {}
                if (self._inbox is not None and (sender.get("sender_type") or sender.get("type")) in {"app", "bot"}
                        and not message.get("deleted") and created.at <= end):
                    await self._inbox.remember_bot(chat_id, created.at)
                if created.before(start) or created.before(time() - _CATCHUP_MAX_AGE):
                    stats["too_old"] += 1
                    continue
                if message.get("deleted") or created.at > end:
                    continue
                event = history_event(message, chat_id, page["chat_type"])
                if bots_only and (event.get("sender_type") not in {"app", "bot"}
                                  or page["chat_type"] != "group" or not self._bot_mentions(event)
                                  or self._own_message(event)):
                    continue
                event["_catchup_created_at"] = created
                outcome = await self._handle_lark_event(event)
                if outcome == "duplicate":
                    stats["duplicates"] += 1
                elif outcome == "handled":
                    stats["handled"] += 1
                elif outcome == "too_old":
                    stats["too_old"] += 1
            except Exception:
                stats["failures"] += 1
                logger.warning("%s: catch-up message failed", self.channel_id, exc_info=True)

    async def _catchup_threads(self, chat_id: str, *, start: float, end: float, stats: dict[str, int],
                               seen: set[str] | None = None, bots_only: bool = False) -> None:
        if not hasattr(self._transport, "list_thread_messages"):
            return
        assert self._inbox is not None
        # Our recent activity takes precedence over new roots when the listing budget fills.
        candidates = dict.fromkeys(await self._inbox.recent_threads(chat_id, end))
        if hasattr(self._transport, "list_threads"):
            token, roots_scanned = "", 0
            try:
                for page_index in range(_CATCHUP_MAX_PAGES):
                    page = await self._transport.list_threads(chat_id, page_token=token)  # type: ignore[attr-defined]
                    old = False
                    for root in page["roots"][:50]:
                        roots_scanned += 1
                        if _history_created(root).before(end - _CATCHUP_ROOT_MAX_AGE):
                            old = True
                            break
                        if thread_id := root.get("thread_id"):
                            candidates.setdefault(thread_id, None)
                    if old or not page.get("has_more"):
                        break
                    if page_index + 1 == _CATCHUP_MAX_PAGES:
                        stats["thread_roots_capped"] += 1
                        logger.info("%s: catch-up roots capped chat=%s roots_scanned=%s "
                                    "unscanned_root_pages_at_least=1", self.channel_id, chat_id, roots_scanned)
                        break
                    next_token = page.get("page_token")
                    if not next_token or next_token == token:
                        raise ValueError("history pagination did not advance")
                    token = next_token
            except Exception:
                stats["failures"] += 1
                logger.warning("%s: catch-up thread discovery failed chat=%s", self.channel_id, chat_id, exc_info=True)
        skipped = len(candidates) - _CATCHUP_MAX_THREADS
        if skipped > 0:
            logger.info("%s: catch-up thread budget chat=%s threads_skipped=%s", self.channel_id, chat_id, skipped)
        slots = asyncio.Semaphore(_CATCHUP_THREAD_CONCURRENCY)

        async def scan(thread_id: str) -> None:
            async with slots:
                await self._catchup_thread(thread_id, chat_id, start=start, end=end, stats=stats,
                                           seen=seen, bots_only=bots_only)

        # TaskGroup joins cancelled listings before the inbox/transport can close.
        async with asyncio.TaskGroup() as group:
            for thread_id in list(candidates)[:_CATCHUP_MAX_THREADS]:
                group.create_task(scan(thread_id))

    async def _catchup_thread(self, thread_id: str, chat_id: str, *, start: float, end: float,
                              stats: dict[str, int], seen: set[str] | None = None,
                              bots_only: bool = False) -> None:
        if not hasattr(self._transport, "list_thread_messages") or (seen is not None and thread_id in seen):
            return
        if seen is not None:
            seen.add(thread_id)
        stats["threads_scanned"] += 1
        pages = []
        try:
            async for page in self._history_pages(
                self._transport.list_thread_messages, thread_id,  # type: ignore[attr-defined]
            ):
                pages.append({**page, "messages": page["messages"][:50]})
                for index, message in enumerate(pages[-1]["messages"]):
                    if _history_created(message).before(start):
                        pages[-1]["messages"] = pages[-1]["messages"][:index + 1]
                        break
                else:
                    continue
                break
        except Exception:
            stats["thread_failures"] += 1
            logger.warning("%s: catch-up thread failed chat=%s thread=%s", self.channel_id, chat_id,
                           thread_id, exc_info=True)
        # Admit already-read pages even when a later page fails, oldest first within the thread.
        for page in reversed(pages):
            await self._catchup_messages(
                {**page, "messages": [{**message, "thread_id": thread_id}
                                      for message in reversed(page["messages"])]}, chat_id,
                start=start, end=end, stats=stats, bots_only=bots_only)

    async def _catch_up(self, boundary: float | None) -> None:
        stats = dict(chats_scanned=0, messages_found=0, handled=0, duplicates=0, too_old=0, failures=0,
                     debt_windows_dropped=0, threads_scanned=0, thread_failures=0, thread_roots_capped=0)
        expired = boundary is not None and time() - boundary > _CATCHUP_MAX_AGE
        try:
            assert self._inbox is not None
            debts = await self._inbox.debts()
            cutoff = time() - _CATCHUP_MAX_AGE
            for chat_id, (start, end) in list(debts.items()):
                if start < cutoff:
                    stats["debt_windows_dropped"] += 1
                    logger.warning("%s: catch-up debt expired chat=%s start=%s end=%s skipped_seconds=%s "
                                   "debt_windows_dropped=1", self.channel_id, chat_id, start, min(end, cutoff),
                                   min(end, cutoff) - start)
                    if end <= cutoff:
                        await self._inbox.complete(chat_id)
                        del debts[chat_id]
                    else:
                        debts[chat_id] = (cutoff, end)
                        await self._inbox.defer(chat_id, cutoff, end)
            discovery = await self._inbox.discovery()
            live_start = boundary - _CATCHUP_OVERLAP if boundary is not None and not expired else None
            if discovery is not None:
                if discovery[1] <= cutoff:
                    await self._inbox.complete_discovery()
                    logger.warning("%s: catch-up discovery expired skipped_windows=1", self.channel_id)
                else:
                    pending_start = max(discovery[0], cutoff)
                    live_start = min(live_start, pending_start) if live_start is not None else pending_start
            if (live_start is None and not debts) or not hasattr(self._transport, "list_messages"):
                return
            chats = await self._catchup_chats()
            # Persist all planned work before awaiting history: live checkpoints can
            # advance independently, even if startup is cancelled halfway through.
            windows: dict[str, float] = {}
            for chat_id in dict.fromkeys([*debts, *chats]):
                if live_start is None and chat_id not in debts:
                    continue
                owed = debts.get(chat_id)
                start = min(live_start, owed[0]) if live_start is not None and owed else (
                    live_start if live_start is not None else owed[0])
                assert start is not None
                windows[chat_id] = start
                await self._inbox.defer(chat_id, start, time())
            if ready := getattr(self._transport, "consumer_started", None):
                await asyncio.wait_for(ready.wait(), timeout=60)
            end = time()
            for chat_id, start in windows.items():
                await self._inbox.defer(chat_id, start, end)
            await self._inbox.complete_discovery()
            for chat_id, start in list(windows.items())[:_CATCHUP_MAX_CHATS]:
                stats["chats_scanned"] += 1
                failures = stats["failures"]
                try:
                    async for page in self._history_pages(
                        self._transport.list_messages, chat_id, start=start, end=end,  # type: ignore[attr-defined]
                    ):
                        await self._catchup_messages(page, chat_id, start=start, end=end, stats=stats)
                    await self._catchup_threads(chat_id, start=start, end=end, stats=stats)
                    if failures == stats["failures"] and await self._settled(chat_id):
                        await self._inbox.complete(chat_id)
                except Exception:
                    stats["failures"] += 1
                    logger.warning("%s: catch-up chat failed chat=%s", self.channel_id, chat_id, exc_info=True)
        except Exception:
            stats["failures"] += 1
            logger.warning("%s: catch-up discovery failed", self.channel_id, exc_info=True)
        finally:
            logger.info("lark_catchup channel=%s boundary=%s boundary_expired=%s %s", self.channel_id, boundary,
                        expired, " ".join(f"{key}={value}" for key, value in stats.items()))

    async def _bound_chats(self) -> list[str]:
        from ...db.chat_bindings import list_bindings

        return [binding["chat_id"] for binding in await list_bindings(self.channel_id)]

    async def _poll_bot_chats(self, last_poll: dict[str, float]) -> None:
        now = time()
        chats = await self._bound_chats()
        assert self._inbox is not None
        recent_bots = await self._inbox.recent_bot_chats(now)
        fast = recent_bots.intersection(chats)
        if self._resolve_chat is not None:
            fast.update(chat_id for chat_id in chats
                        if (route := self._resolve_chat(self.channel_id, chat_id)).explicit
                        and route.sender_policy == SenderPolicy.CHAT)
        logged_at = monotonic()
        if self._bot_poll_logged_at is None or logged_at - self._bot_poll_logged_at >= _BOT_POLL_LOG_SECONDS:
            logger.info("lark_bot_poll_cadence channel=%s fast=%s slow=%s", self.channel_id,
                        len(fast), len(chats) - len(fast))
            self._bot_poll_logged_at = logged_at
        for removed in last_poll.keys() - set(chats):
            del last_poll[removed]
        for chat_id in chats:
            end = time()
            interval = _BOT_POLL_SECONDS if chat_id in fast else _BOT_SLOW_POLL_SECONDS
            if chat_id in last_poll and end - last_poll[chat_id] < interval:
                continue
            # Save the first boundary before querying so a failed listing keeps its window.
            boundary = last_poll.setdefault(chat_id, now - interval)
            start = max(boundary - _CATCHUP_OVERLAP,
                        end - _CATCHUP_MAX_AGE)
            stats = dict(messages_found=0, handled=0, duplicates=0, too_old=0, failures=0,
                         threads_scanned=0, thread_failures=0, thread_roots_capped=0)
            try:
                seen: set[str] = set()
                thread_activity = await self._inbox.has_thread_activity(chat_id, end - _CATCHUP_ROOT_MAX_AGE)
                async for page in self._history_pages(
                    self._transport.list_messages, chat_id, start=start, end=end,  # type: ignore[attr-defined]
                ):
                    await self._catchup_messages(page, chat_id, start=start, end=end, stats=stats, bots_only=True)
                    if page["chat_type"] == "group":
                        for message in _history_messages(page["messages"][:50]):
                            if thread_id := message.get("thread_id"):
                                thread_activity = True
                                await self._catchup_thread(thread_id, chat_id, start=start, end=end,
                                                           stats=stats, seen=seen, bots_only=True)
                if thread_activity:
                    await self._catchup_threads(chat_id, start=start, end=end, stats=stats, seen=seen, bots_only=True)
                if not stats["failures"] and not stats["thread_failures"] and await self._settled(chat_id):
                    last_poll[chat_id] = end
                else:
                    logger.warning("%s: bot poll failed chat=%s failures=%s thread_failures=%s",
                                   self.channel_id, chat_id, stats["failures"], stats["thread_failures"])
            except Exception:
                logger.warning("%s: bot poll failed chat=%s", self.channel_id, chat_id, exc_info=True)

    async def _bot_poll_loop(self) -> None:
        if not hasattr(self._transport, "list_messages"):
            return
        last_poll: dict[str, float] = {}
        while self._running:
            await asyncio.sleep(_BOT_POLL_SECONDS)
            try:
                await self._poll_bot_chats(last_poll)
            except Exception:
                logger.warning("%s: bot poll discovery failed", self.channel_id, exc_info=True)

    def _own_message(self, event: dict[str, Any]) -> bool:
        identities = {identity for identity in (self._bot_open_id, self._bot_app_id) if identity}
        return any(isinstance(event.get(key), str) and event[key] in identities
                   for key in ("sender_id", "sender_app_id"))

    async def _heartbeat(self) -> None:
        while self._running:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            ready = getattr(self._transport, "consumer_started", None)
            if (self._inbox is not None
                    and not self._ingress_lock.locked() and not self._unsettled
                    and ready is not None and ready.is_set()):
                try:
                    await self._inbox.checkpoint(time())
                except Exception:
                    logger.warning("%s: could not save consumer heartbeat", self.channel_id, exc_info=True)

    async def _event_collector_loop(self) -> None:
        """Restart each ended or broken event stream after a delay."""
        failures = 0
        while self._running:
            started = monotonic()
            self._stream_yielded = False
            try:
                await self._run_event_consumer()
            except asyncio.CancelledError:
                break
            except Exception:
                if self._backoff:
                    logger.exception("%s event collector crashed", self.channel_id)
                else:
                    logger.exception("%s event collector crashed, restarting in %ss", self.channel_id, self._RESTART_SECONDS)
            else:
                if self._running and not self._backoff:
                    logger.warning("%s event consumer stopped; starting it again in %ss", self.channel_id, self._RESTART_SECONDS)
            delay = self._RESTART_SECONDS
            if self._backoff:
                if self._stream_yielded or monotonic() - started >= 6 * self._RESTART_SECONDS:
                    failures = 0
                else:
                    # Five failures reach the 30x cap; keep the exponent bounded after that.
                    failures = min(failures + 1, 5)
                delay *= min(2 ** failures, 30) * _reconnect_jitter(0.5, 1.0)
            if self._running and self._backoff:
                logger.warning("%s event consumer stopped; starting it again in %ss", self.channel_id, delay)
            await asyncio.sleep(delay)

    async def _run_event_consumer(self) -> None:
        """Handle one event stream; a malformed event does not stop its connection."""
        events = self._transport.events()
        try:
            async for event in events:
                self._stream_yielded = True
                if not self._running:
                    break
                try:
                    await self._handle_lark_event(event)
                except Exception:
                    logger.debug("%s: skipping an invalid event", self.channel_id, exc_info=True)
        finally:
            if close := getattr(events, "aclose", None):
                await close()

    async def _remember_thread(self, chat_id: str, thread_id: str) -> None:
        if self._inbox is not None:
            await self._inbox.remember_thread(chat_id, thread_id, time())

    async def _handle_lark_event(self, event: dict[str, Any]) -> Literal["handled", "duplicate", "ignored", "too_old"]:
        async with self._ingress_lock:
            message_id = event.get("message_id")
            if message_id in self._unsettled or (
                    self._inbox is not None and message_id and await self._inbox.processed(message_id)):
                return "duplicate"
            created = event.get("_catchup_created_at")
            if created is not None and created.before(time() - _CATCHUP_MAX_AGE):
                return "too_old"
            if (self._inbox is not None and event.get("type") == RECEIVE_EVENT
                    and created is None and event.get("sender_type") in {"app", "bot"} and event.get("chat_id")):
                await self._inbox.remember_bot(event["chat_id"], time())
            if self._own_message(event):
                event["_bot_outcome"] = "own_message"
                handled = False
            else:
                handled = await self._process_lark_event(event)
            if event.get("sender_type") in {"app", "bot"}:
                logger.info("lark_bot_message chat=%s bot_id=%s message_id=%s outcome=%s",
                            event.get("chat_id"), event.get("sender_id"), message_id,
                            "handled" if handled else event.get("_bot_outcome", "ignored"))
            if (self._inbox is not None and message_id and event.get("type") == RECEIVE_EVENT
                    and not event.get("_deferred")):  # recorded once published, or held to be read again
                await self._inbox.finish(message_id, chat_id=event.get("chat_id") or "",
                                   thread_id=(event.get("thread_id") or "") if handled else "", at=time())
            return "handled" if handled else "ignored"

    async def _process_lark_event(self, event: dict[str, Any]) -> bool | None:
        """Publish text, rich posts and merged forwards to the Bus, with what they quote, and answer other admitted
        message types. The event is lark-cli's flat one: ``chat_id``,
        ``chat_type`` (``p2p`` or ``group``), ``message_id``, ``message_type``, ``sender_id`` (the open_id),
        ``sender_type``, ``mentions`` (each ``id``, an open_id, ``key`` and ``name``), ``content``, a text
        message's text with each mention shown as ``@name``, for a reply ``reply_to`` and ``root_id``, and in a
        thread ``thread_id``."""
        assert self._bus is not None

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
        # The bot stream already decided admission before queueing; CLI events decide here.
        admitted = event.get("admitted")
        decision = (self._resolve_chat(self.channel_id, chat_id)
                    if admitted is not False and self._resolve_chat is not None else None)
        if not group and decision is not None:
            decision = replace(decision, sender_policy=SenderPolicy.LISTED)
        chat_senders = decision.senders if decision is not None else frozenset()
        bot = event.get("sender_type") in {"app", "bot"}
        chat_policy = group and decision is not None and decision.explicit and decision.sender_policy == "chat"
        log_refusal = True
        if bot:
            now = monotonic()
            self._bot_refusals = {key: at for key, at in self._bot_refusals.items()
                                  if now - at < _BOT_WINDOW_SECONDS}
            log_refusal = (chat_id, str(sender_open_id)) not in self._bot_refusals
        if admitted is False and log_refusal:
            logger.warning("%s: dropped a message from sender %r, refused by the stream prefilter",
                           self.channel_id, str(sender_open_id or "<unknown>")[:80])
        if admitted is False or (admitted is not True and not chat_policy and not self._allowlist.admits(
                sender_open_id, chat_senders=chat_senders, log_refusal=log_refusal)):
            if bot:
                event["_bot_outcome"] = "refused"
                if log_refusal:
                    self._bot_refusals[(chat_id, str(sender_open_id))] = monotonic()
            if self._on_rejected is not None and isinstance(sender_open_id, str) and sender_open_id:
                try:
                    await self._on_rejected(sender_open_id, chat_id if group else None)
                except Exception:
                    logger.warning("%s: could not record a rejected sender", self.channel_id, exc_info=True)
            return

        content = event.get("content")
        content = content if isinstance(content, str) else ""
        message_type = event.get("message_type", "")
        inbound = lark_content.parse(message_type, content, bot_mentions)
        text = inbound.text
        if inbound.kind in (lark_content.ContentKind.TEXT, lark_content.ContentKind.RICH):
            if not content.strip():
                return
            if not text.strip() and not (event.get("reply_to") or event.get("root_id")):
                return

        message_id = event.get("message_id") or ""
        if group and message_id and event.get("thread_id"):
            self._thread_messages[message_id] = None
            while len(self._thread_messages) > _THREAD_MESSAGES_KEPT:
                del self._thread_messages[next(iter(self._thread_messages))]
        manager_id, explicit = self._manager_id, False
        metadata = {"origin_channel_id": self.channel_id}
        if decision is not None:
            manager_id, explicit = decision.manager_id, decision.explicit
            silent = decision.action is RouteAction.SILENT
            if not silent:
                metadata["channel_thread_id"] = channel_thread_id(self.channel_id, chat_id, manager_id, self._manager_id)
                metadata["channel_manager_id"] = self._manager_id
            await observe_chat(self.channel_id, chat_id, suppressed=silent)
            if silent:
                log_route(logger, self.channel_id, chat_id, decision)
                return
        from ..tiers.manager import is_stop_command

        if bot:
            now = monotonic()
            turns = self._bot_turns.setdefault(chat_id, deque())
            while turns and now - turns[0] >= _BOT_WINDOW_SECONDS:
                turns.popleft()
            if len(turns) >= _BOT_TURNS_PER_HOUR:
                logger.warning("%s: bot turn limit chat=%s count=%s", self.channel_id, chat_id, len(turns) + 1)
                event["_bot_outcome"] = "limited"
                return
        if await self._turned_off(chat_id, message_id if group else None, manager_id=manager_id,
                                  explicit=explicit, stop=is_stop_command(text)):
            return
        if bot:
            turns.append(now)
        log_route(logger, self.channel_id, chat_id, decision or RouteDecision(RouteAction.ADMIT, manager_id, explicit),
                  via_chat_senders=sender_open_id not in self._allowlist.ids)
        if inbound.kind is lark_content.ContentKind.UNSUPPORTED:
            logger.info("%s: answered an unsupported %s message in %s", self.channel_id,
                        message_type or "unknown", chat_id)
            reply = lark_content.UNSUPPORTED_REPLY.format(kind=message_type or "unknown")
            try:
                if group:
                    await self._reply(message_id, text=reply)
                else:
                    await self._transport.send(chat_id=chat_id, text=reply)
            except LarkCliError:
                logger.warning("%s: could not answer an unsupported message in %s", self.channel_id,
                               chat_id, exc_info=True)
            return True
        self._reactions.add(chat_id, message_id)  # the bot saw it; in the background, the Manager gets it now
        stop = is_stop_command(text)
        delivery = _Delivery(event, chat_id, group, message_id, text, inbound, manager_id, metadata,
                             sender_open_id or "", [] if stop else self._quote_targets(
                                 event, message_id, forward=inbound.kind is lark_content.ContentKind.FORWARD))
        if stop:
            # Claimed at once: this chat's messages still being read go first, as withdrawn, so none becomes a turn.
            await self._withdraw(chat_id)
        elif chat_id in self._held_chats:
            # Behind a message that could not be published: left unrecorded, read again in order after a restart.
            logger.warning("%s: held message %s behind a message not delivered in %s", self.channel_id, message_id,
                           chat_id)
            event["_deferred"] = True
            return True
        elif delivery.targets or chat_id in self._chat_tail:
            # Read in the background, behind this chat's earlier messages: other chats and stops do not wait.
            self._schedule(delivery)
            event["_deferred"] = True
            return True
        await self._bus.publish(self._envelope(delivery, *await self._prepare(delivery)))
        return True

    async def _prepare(self, delivery: _Delivery) -> tuple[list[Quote], list[Attachment], str]:
        """What the message's envelope needs read: its quotes, its and their resources (the ones not received are
        told in one reply), and in a group its sender's name."""
        message_id = delivery.message_id
        quoted: list[Quote] = []
        quoted_resources: tuple[lark_content.ResourceRef, ...] = ()
        if delivery.busy:
            quoted = self._unread(delivery, "busy")
        elif delivery.targets:
            try:
                quoted, quoted_resources = await self._quote(delivery)
            except Exception:
                logger.warning("%s: could not read what message %s quotes", self.channel_id, message_id,
                               exc_info=True)
                quoted = self._unread(delivery, "internal error")
        resources = delivery.inbound.resources + quoted_resources
        attachments = await self._collect(message_id, resources) if resources else []
        if any(item.state is not AttachmentState.SAVED for item in attachments):
            try:
                reply = _not_received(attachments)
                if delivery.group:
                    await self._reply(message_id, text=reply)
                else:
                    await self._transport.send(chat_id=delivery.chat_id, text=reply)
                attachments = [replace(item, told=True) if item.state is not AttachmentState.SAVED else item
                               for item in attachments]
            except LarkCliError:
                logger.warning("%s: could not report attachments not received", self.channel_id, exc_info=True)
        name = await self._sender_name(delivery.sender) if delivery.group else ""
        return quoted, attachments, name

    @staticmethod
    def _unread(delivery: _Delivery, why: str) -> list[Quote]:
        """The message's quotes, each saying why it was not read."""
        return [lark_quote.build(kind, ref, [], error=why)[0] for kind, ref in delivery.targets]

    def _envelope(self, delivery: _Delivery, quoted: list[Quote], attachments: list[Attachment],
                  sender_name: str, *, withdrawn: bool = False) -> MessageEnvelope:
        """The message's envelope, with its reply routing registered: built once, however often it is published."""
        import uuid as _uuid

        chat_id, message_id, event = delivery.chat_id, delivery.message_id, delivery.event
        correlation_id = str(_uuid.uuid4())
        self._correlation_chat[correlation_id] = chat_id
        context = {"chat_id": chat_id, "chat_type": "group" if delivery.group else "p2p", "sender_id": delivery.sender,
                   "message_id": message_id}
        # What the message replies to: the message it answers and the first of its thread (a gate's messages).
        for key in ("reply_to", "root_id", "thread_id"):
            if isinstance(ref := event.get(key), str) and ref:
                context[key] = ref
        if delivery.group:
            if message_id:
                self._group_message[correlation_id] = message_id
            context["sender_name"] = sender_name

        envelope = MessageEnvelope.create(
            source=self._address,
            target=manager_address(delivery.manager_id),
            message_type=MessageType.USER_MESSAGE,
            payload={
                "text": delivery.text,
                # The user stopped it before it was read: the Manager records it, never as a turn or a gate reply.
                **({"withdrawn": True} if withdrawn else {}),
                **({"quoted": [quote.to_payload() for quote in quoted]} if quoted else {}),
                **({"attachments": [item.to_payload() for item in attachments]} if attachments else {}),
                "language": "zh",
                "channel_context": context,
            },
            correlation_id=correlation_id,
            metadata=delivery.metadata,
        )
        return envelope

    def _schedule(self, delivery: _Delivery) -> None:
        pending = [item for item in self._unsettled.values() if item.state is _DeliveryState.PENDING]
        if delivery.targets and (len(pending) >= _PENDING_MAX or sum(
                item.chat_id == delivery.chat_id for item in pending) >= _CHAT_PENDING_MAX):
            delivery.busy = True
            logger.warning("%s: too many messages waiting; message %s goes without its quotes read",
                           self.channel_id, delivery.message_id)
        delivery.previous = self._chat_tail.get(delivery.chat_id)
        self._unsettled[delivery.message_id] = delivery
        self._chat_tail[delivery.chat_id] = delivery
        delivery.task = asyncio.create_task(self._deliver_later(delivery))

        def done(_: asyncio.Task[None]) -> None:
            if self._chat_tail.get(delivery.chat_id) is delivery:
                del self._chat_tail[delivery.chat_id]

        delivery.task.add_done_callback(done)

    async def _deliver_later(self, delivery: _Delivery) -> None:
        """Read what the message needs (at most ``_DELIVERY_CONCURRENCY`` at once), unless a stop withdraws it; once
        its chat's previous message is recorded, publish it and write its receipt, each tried again after
        ``_DELIVERY_RETRY_SECONDS``. A message that cannot be published or recorded, or whose previous one was
        held, is held: it stays unrecorded and unsettled, so the restart boundary stays before it and the next
        start reads it again, in order with the chat's later messages, which are held too."""
        async def bounded() -> tuple[list[Quote], list[Attachment], str]:
            async with self._delivery_slots:
                return await self._prepare(delivery)

        work = asyncio.create_task(bounded())
        withdrawn = asyncio.create_task(delivery.withdraw.wait())
        try:
            await asyncio.wait({work, withdrawn}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (work, withdrawn):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, withdrawn, return_exceptions=True)
        if delivery.withdraw.is_set():
            prepared: tuple[list[Quote], list[Attachment], str] = ([], [], delivery.sender)
        elif not work.cancelled() and work.exception() is None:
            prepared = work.result()
        else:
            logger.warning("%s: could not read for message %s", self.channel_id, delivery.message_id,
                           exc_info=work.exception())
            prepared = (self._unread(delivery, "internal error"), [], delivery.sender)
        previous, delivery.previous = delivery.previous, None  # no chain of finished messages is kept
        if previous is not None:
            if previous.task is not None:
                await asyncio.gather(previous.task, return_exceptions=True)
            if previous.state is not _DeliveryState.RECORDED:
                return self._hold(delivery, "an earlier message in its chat was not delivered")
        envelope = self._envelope(delivery, *prepared, withdrawn=delivery.withdraw.is_set())
        assert self._bus is not None
        if not await self._retried(lambda: self._bus.publish(envelope), "publish", delivery):
            return self._hold(delivery, "it could not be published")
        delivery.state = _DeliveryState.PUBLISHED
        if self._inbox is not None and delivery.event.get("type") == RECEIVE_EVENT:
            inbox = self._inbox
            if not await self._retried(lambda: inbox.finish(
                    delivery.message_id, chat_id=delivery.chat_id,
                    thread_id=delivery.event.get("thread_id") or "", at=time()), "record", delivery):
                # Published, not recorded: read again after a restart, it can reach the Manager twice.
                return self._hold(delivery, "its receipt could not be written")
        delivery.state = _DeliveryState.RECORDED
        self._unsettled.pop(delivery.message_id, None)

    async def _retried(self, attempt: Callable[[], Awaitable[Any]], what: str, delivery: _Delivery) -> bool:
        for delay in (*_DELIVERY_RETRY_SECONDS, None):
            try:
                await attempt()
                return True
            except Exception:
                logger.warning("%s: could not %s message %s%s", self.channel_id, what, delivery.message_id,
                               f"; trying again in {delay:g} s" if delay is not None else "", exc_info=True)
                if delay is None:
                    return False
                await asyncio.sleep(delay)
        return False

    def _hold(self, delivery: _Delivery, why: str) -> None:
        delivery.state = _DeliveryState.HELD
        self._held_chats.add(delivery.chat_id)
        logger.error("lark_delivery_held channel=%s chat=%s message=%s reason=%s: it and the chat's later messages "
                     "stay unrecorded, to be read again after a restart", self.channel_id, delivery.chat_id,
                     delivery.message_id, why)

    async def _settled(self, chat_id: str) -> bool:
        """Whether the chat's messages read in the background are all recorded, once those still running end."""
        running = [delivery.task for delivery in list(self._unsettled.values())
                   if delivery.chat_id == chat_id and delivery.task is not None and not delivery.task.done()]
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        return not any(delivery.chat_id == chat_id for delivery in self._unsettled.values())

    async def _withdraw(self, chat_id: str) -> None:
        """Publish this chat's messages still being read at once, as withdrawn, in order."""
        pending = [delivery for delivery in self._unsettled.values()
                   if delivery.chat_id == chat_id and delivery.state is _DeliveryState.PENDING]
        for delivery in pending:
            delivery.withdraw.set()
        tasks = [delivery.task for delivery in pending if delivery.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _quote_targets(self, event: dict[str, Any], message_id: str, *,
                       forward: bool) -> list[tuple[QuoteKind, str]]:
        """What the message quotes: a merged forward itself, else the message it replies to, and in a thread, when
        that is not the thread's first message, the first message (with the latest replies); none when the
        transport cannot read messages."""
        if not hasattr(self._transport, "get_message"):
            return []
        reply_to, root_id, thread_id = (value if isinstance(value := event.get(key), str) else ""
                                        for key in ("reply_to", "root_id", "thread_id"))
        wanted = [(QuoteKind.FORWARD, message_id)] if forward else [(QuoteKind.REPLY, reply_to)] if reply_to else []
        if (reply_to and root_id and root_id != reply_to and thread_id
                and hasattr(self._transport, "list_thread_messages")):
            wanted.append((QuoteKind.THREAD, root_id))
        return wanted

    async def _quote(self, delivery: _Delivery) -> tuple[list[Quote], tuple[lark_content.ResourceRef, ...]]:
        """The message's quotes (``lark_quote``), read as the bot within ``_QUOTE_SECONDS``; a read that fails
        becomes a line saying why."""
        read = self._transport.get_message  # type: ignore[attr-defined]
        event, message_id, wanted = delivery.event, delivery.message_id, delivery.targets
        after = len(delivery.inbound.resources)
        reply_to, thread_id = str(event.get("reply_to") or ""), str(event.get("thread_id") or "")
        deadline = monotonic() + _QUOTE_SECONDS

        async def fetch(kind: QuoteKind, ref: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
            try:
                async with asyncio.timeout(deadline - monotonic()):
                    if kind is not QuoteKind.THREAD:
                        return await read(ref, timeout=deadline - monotonic()), [], ""
                    root, page = await asyncio.gather(
                        read(ref, timeout=deadline - monotonic()),
                        self._transport.list_thread_messages(thread_id),  # type: ignore[attr-defined]
                        return_exceptions=True)
                    for result in (root, page):
                        if isinstance(result, BaseException):
                            raise result
                    shown = {message_id, ref, reply_to}
                    replies = [item for item in page["messages"] if item.get("message_id") not in shown]
                    return root, replies[:lark_quote.THREAD_TAIL][::-1], ""
            except (TimeoutError, LarkCliError) as exc:
                return [], [], lark_quote.failure(exc)
            except Exception:
                logger.warning("%s: could not read quoted message %s", self.channel_id, ref, exc_info=True)
                return [], [], "error"

        read_all = await asyncio.gather(*(fetch(kind, ref) for kind, ref in wanted))
        names: dict[str, str] = {}
        if ids := lark_quote.sender_ids(item for items, thread, _ in read_all for item in (*items, *thread)):
            try:
                names = await asyncio.wait_for(self._transport.names(ids),
                                               max(min(_NAME_LOOKUP_SECONDS, deadline - monotonic()), 0.1))
            except (TimeoutError, LarkCliError):
                names = {}
        quoted: list[Quote] = []
        resources: list[lark_content.ResourceRef] = []
        for (kind, ref), (items, thread, error) in zip(wanted, read_all):
            quote, found = lark_quote.build(kind, ref, items, thread=thread, names=names, error=error,
                                            own=(self._bot_open_id, self._bot_app_id), after=after + len(resources))
            quoted.append(quote)
            resources += found
            logger.info("lark_quote channel=%s message=%s quoted=%s kind=%s type=%s messages=%d lines=%d chars=%d "
                        "resources=%d outcome=%s", self.channel_id, message_id, ref, kind.value,
                        quote.message_type or "unknown", quote.messages, len(quote.lines),
                        sum(len(line) for line in quote.lines), len(found), error or "read")
        return quoted, tuple(resources)

    async def _collect(self, message_id: str, refs: tuple[lark_content.ResourceRef, ...]) -> list[Attachment]:
        deadline = monotonic() + _DOWNLOAD_MESSAGE_SECONDS
        try:
            directory = files.message_dir(self._runtime_root, self.channel_id, message_id)
        except (OSError, ValueError):
            directory = None
        attachments = []
        for ref in refs:
            kind = AttachmentKind(ref.kind.value)
            item = Attachment(ref.index, kind, AttachmentState.FAILED,
                              files.safe_name(ref.name, kind.value), self.channel_type)
            part = directory / f"{ref.index}.part" if directory else None
            try:
                remaining = deadline - monotonic()
                if ref.index > files.PER_MESSAGE_MAX:
                    item = replace(item, state=AttachmentState.OVER_LIMIT)
                elif remaining <= 0:
                    item = replace(item, reason="timed out")
                elif part is None:
                    item = replace(item, reason="could not be saved")
                else:
                    maximum = files.IMAGE_MAX_BYTES if kind is AttachmentKind.IMAGE else files.FILE_MAX_BYTES
                    await asyncio.wait_for(self._transport.download(ref.message_id or message_id, ref, part,
                                                                    max_bytes=maximum), min(30, remaining))
                    item = files.accept(part, index=ref.index, kind=kind, name=ref.name,
                                        source=self.channel_type, max_bytes=maximum)
            except TooLarge:
                item = replace(item, state=AttachmentState.TOO_LARGE)
            except TimeoutError:
                item = replace(item, reason="timed out")
            except LarkCliError as exc:
                item = replace(item, reason=str(exc)[:200])
            except OSError:
                item = replace(item, reason="could not be saved")
            finally:
                if part is not None:
                    try:
                        part.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("%s: could not remove an attachment partial file", self.channel_id, exc_info=True)
            logger.info("%s: attachment %s message=%s index=%d kind=%s size=%s reason=%s", self.channel_id,
                        item.state.value, message_id, item.index, item.kind.value, item.size, item.reason)
            attachments.append(item)
        return attachments

    # --- Outbound: Bus → Lark ---

    async def _outbound_loop(self) -> None:
        assert self._bus is not None
        try:
            async for envelope in self._bus.subscribe(self.channel_id):
                if not self._running:
                    break
                # An answer is stored once it was sent, or as not delivered (the Manager names its record).
                await record_reply(self.channel_type, envelope, await self.deliver(envelope),
                                   runtime_root=self._runtime_root)
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

    async def _sender_name(self, open_id: str) -> str:
        """A group sender's name, as the allowlist resolves it; their open_id when it cannot be found in time."""
        try:
            found = await asyncio.wait_for(self._transport.names([open_id]), _NAME_LOOKUP_SECONDS)
        except (TimeoutError, LarkCliError):
            found = {}
        return found.get(open_id) or open_id
