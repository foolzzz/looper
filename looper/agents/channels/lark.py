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
import uuid as _uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from random import uniform as _reconnect_jitter
from time import monotonic, time
from typing import Any, Literal

from ..bus import MessageBus
from .. import card_actions
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
from ..task_notice import Notice, NotifyTarget, TargetKind, split_message
from .. import task_notice_delivery
from ..ops.leads import for_group
from .. import chat_settings
from .allowlist import SenderAllowlist
from .base import ChannelAdapter, DeliveryFailure, DeliveryReceipt, PostOutcome, PostReceipt
from .chat_history import record_reply
from .proactive import record_delivery
from .chat_routing import ChatResolver, RouteAction, RouteDecision, SenderPolicy, observe_chat, log_route
from . import lark_content, lark_quote
from .lark_cli import MESSAGE_GONE_CODES, RECEIVE_EVENT, LarkCliError, LarkCliOutputError, mention
from .lark_transport import CliTransport, LarkTransport, LarkTransportKind, GatedLarkTransport
from .lark_inbox import CatchupDropReason, LarkInbox, history_event
from .lark_quote import QuoteKind, QuoteRead, QuoteRef
from .status_line import Reactions, StatusKind, StatusLines, status_kind

logger = logging.getLogger(__name__)

# Told the open_id of a sender the allowlist refused and the group chat they wrote in (None for a direct
# message); never what they wrote.
RejectedListener = Callable[[str, str | None], Awaitable[None]]

# How long finding a group sender's name may hold up their message; then the Manager gets the open_id.
_NAME_LOOKUP_SECONDS = 5.0
# How long reading what a message quotes may take, names included; then it goes on with what was read. A long
# merged forward's raw read fails after about 5 s and its rendered read then takes about 6 s.
_QUOTE_SECONDS = 15.0
# Group messages in a thread, whose answers go in the thread: the latest ones kept.
_THREAD_MESSAGES_KEPT = 1_000
_DOWNLOAD_MESSAGE_SECONDS = 60.0
_CATCHUP_MAX_AGE = 15 * 60
_CATCHUP_RETRY_DELAYS = (30, 60, 120, 300, 600)
_CATCHUP_MAX_ATTEMPTS = 10
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


class _HistoryFailure(ValueError):
    def __init__(self, reason: CatchupDropReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _terminal_history_failure(error: Exception) -> CatchupDropReason | None:
    if isinstance(error, _HistoryFailure):
        return error.reason
    if (isinstance(error, PermissionError) or isinstance(error, LarkCliError)
            and (error.code in {lark_quote.MISSING_SCOPE_CODE, 230002}
                 or getattr(error, "http_status", None) == 403 or error.error_type in {"permission", "forbidden"})):
        return CatchupDropReason.PERMISSION
    return None


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


def _check_lark_cli_available() -> bool:
    """Check if lark-cli is available for event collection."""
    import shutil
    return bool(shutil.which("lark-cli"))


class CardWrites:
    """Every card write of a bot-token channel (send, reply, update) in one place. Given the outbound gateway
    (``gateway``: ``write(kind, call, keyed=)`` and ``edit(message_id, call)``) and its switch (``enabled``), each
    write goes through it as the channel's text writes do; without one, straight to the transport."""

    def __init__(self, transport: Any, *, gateway: Any = None, enabled: Callable[[], bool] = lambda: False) -> None:
        self._transport = transport
        self._gateway = gateway
        self._enabled = enabled

    def _gated(self) -> bool:
        return self._gateway is not None and self._enabled()

    async def send(self, card: dict[str, Any], *, chat_id: str | None = None, user_id: str | None = None,
                   idempotency_key: str | None = None) -> tuple[str | None, str | None]:
        """The message the card became and the chat it is in."""
        async def direct() -> tuple[str | None, str | None]:
            return await self._transport.send_card(card, chat_id=chat_id, user_id=user_id,
                                                   idempotency_key=idempotency_key)
        return await self._gateway.write("send", direct, keyed=idempotency_key is not None) if self._gated() \
            else await direct()

    async def reply(self, message_id: str, card: dict[str, Any], *, idempotency_key: str | None = None,
                    reply_in_thread: bool = False) -> tuple[str | None, str | None]:
        async def direct() -> tuple[str | None, str | None]:
            return await self._transport.reply_card(message_id, card, idempotency_key=idempotency_key,
                                                    reply_in_thread=reply_in_thread)
        return await self._gateway.write("reply", direct, keyed=idempotency_key is not None) if self._gated() \
            else await direct()

    async def update(self, message_id: str, card: dict[str, Any]) -> None:
        async def direct() -> None:
            await self._transport.update_card(message_id, card)
        if self._gated():
            await self._gateway.edit(message_id, direct)
        else:
            await direct()


class LarkChannelAdapter(ChannelAdapter):
    """Bridge a Lark transport and the agent message bus."""

    _RESTART_SECONDS = 10.0
    _catchup_retry_wait = staticmethod(asyncio.sleep)

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
        from .. import chat_settings
        from .outbound import Gateway, Outbox
        from ...db import channel_outbox

        self._gateway = Gateway(self._channel_id, app_id=bot_app_id)
        self._channel_outbox = Outbox(self._channel_id, self._transport, self._gateway, channel_outbox,
                                      enabled=chat_settings.outbound_enabled)
        # Cards share the gateway while retaining the bot transport's card methods.
        self._cards = CardWrites(self._transport, gateway=self._gateway, enabled=chat_settings.outbound_enabled) \
            if hasattr(self._transport, "send_card") else None
        self._transport = GatedLarkTransport(self._transport, self._gateway,
                                             enabled=chat_settings.outbound_enabled, outbox=self._channel_outbox)
        self._allowlist = allowlist  # open_ids
        self._profile = profile  # lark-cli's --profile; "" for its active one
        # The bot's own open_id: a group message is handled only when it @-mentions this; none, none is.
        self._bot_open_id = bot_open_id
        self._bot_app_id = bot_app_id
        # Delivery records are resident-only and bounded. Restart/eviction safely restores normal reads.
        self._quote_deliveries: dict[str, lark_quote.OwnDelivery] = {}
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
        self._catchup_retry_task: asyncio.Task[Any] | None = None
        self._catchup_status: dict[str, Any] = {"pending": 0, "dropped": 0, "windows": []}
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._bot_poll_task: asyncio.Task[Any] | None = None
        self._bot_poll_logged_at: float | None = None
        self._inbox: LarkInbox | None = None
        self._ingress_lock = asyncio.Lock()
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
            if self._transport.kind is LarkTransportKind.CLI:
                self._transport.inner.on_thread_activity = self._remember_thread
            boundary = self._inbox.boundary
            # Discovery itself can fail or be cancelled while live checkpoints advance.
            if boundary is not None:
                await self._inbox.defer_discovery(boundary - _CATCHUP_OVERLAP, time())
            # Keep the live boundary until the consumer is healthy.
            await self._inbox.checkpoint(boundary if boundary is not None else time())
            self._catchup_status = await self._inbox.catchup_status()
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
        await self._channel_outbox.start_if_needed()
        self._bus = bus
        self._running = True
        self._outbound_task = asyncio.create_task(self._outbound_loop())
        self._collector_task = asyncio.create_task(self._event_collector_loop())
        if self._inbox is not None:
            self._catchup_task = asyncio.create_task(self._startup_catchup(boundary))
            self._heartbeat_task = asyncio.create_task(self._heartbeat())
            self._bot_poll_task = asyncio.create_task(self._bot_poll_loop())
        logger.info("%s adapter started", self.channel_id)

    async def stop(self) -> None:
        # Cancellation during admission/download must not move the recovery boundary past that message.
        ready = getattr(self._transport, "consumer_started", None)
        clean = (not self._ingress_lock.locked()
                 and ready is not None and ready.is_set())
        stopped_at = time()
        self._running = False
        for task in (self._bot_poll_task, self._heartbeat_task, self._catchup_task, self._catchup_retry_task,
                     self._outbound_task, self._collector_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._reactions.stop()
        if self._transport.kind is LarkTransportKind.CLI:
            self._transport.inner.on_thread_activity = None
        await self._channel_outbox.stop()
        await self._transport.aclose()
        if self._inbox is not None:
            if clean:
                await self._inbox.checkpoint(stopped_at)
            await self._inbox.close()
            self._inbox = None
        logger.info("%s adapter stopped", self.channel_id)

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        source = ("notice" if envelope.message_type is MessageType.CHAT_NOTICE else
                  "progress" if envelope.message_type is MessageType.STATUS_UPDATE else "answer")
        raw_route = envelope.metadata.get("reply_route")
        route = raw_route if isinstance(raw_route, dict) else {}
        chat = self._correlation_chat.get(envelope.correlation_id) or route.get("chat_id")
        with self._transport.message_scope(envelope.message_id, source=source, chat=chat):
            with lark_quote.capture_delivery() as items:
                receipt = await task_notice_delivery.send(envelope, self._deliver)
            session = lark_quote.QuoteSession.from_payload(envelope.metadata.get("quote_session"))
            if (receipt.success and session is not None and session.manager_id == envelope.source.agent_id
                    and envelope.source.agent_type == "manager" and envelope.message_type is MessageType.AGENT_REPLY
                    and not envelope.metadata.get("cards") and len(items) == 1):
                group = envelope.correlation_id in self._group_message or bool(route.get("reply_to"))
                delivery = lark_quote.own_delivery(session, items[0], own=(self._bot_open_id, self._bot_app_id),
                                                   group=group)
                if delivery is not None and delivery.chat_id == chat:
                    self._quote_deliveries[delivery.message_id] = delivery
                    while len(self._quote_deliveries) > _THREAD_MESSAGES_KEPT:
                        del self._quote_deliveries[next(iter(self._quote_deliveries))]
            return receipt

    async def _deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Show a phase on the message's status line, send task progress or a task's end notice as a message of
        its own, or send the answer to the chat the message came from and close its status line."""
        envelope = replace(envelope, payload={
            key: files.redact(value, self._runtime_root, paths=envelope.metadata.get("attachment_paths", ()))
            if isinstance(value, str) else value for key, value in envelope.payload.items()
        })
        receipt_kwargs = ({"return_id": True} if chat_settings.metrics_enabled()
                          and isinstance(envelope.metadata.get("proactive"), dict) else {})
        if envelope.message_type is MessageType.CHAT_NOTICE:
            # A task's end, to who it recorded, in order behind its progress.
            notice = Notice.from_payload(envelope.payload)
            if notice is None:
                return DeliveryReceipt(channel=self.channel_type, success=False, error="a notice without a Lark target")
            return await self._send_notice(notice.target,
                                           for_group(notice.text) if notice.target.group else notice.text,
                                           **receipt_kwargs)
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
                            return await self._send_message(route["chat_id"], str(envelope.payload["text"]),
                                                            **receipt_kwargs)
                        return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc),
                                   outcome=(PostOutcome.REJECTED if exc.refused
                                            else PostOutcome.UNKNOWN))
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
                    return await self._send_message(chat_id, str(envelope.payload["text"]), **receipt_kwargs)
            return DeliveryReceipt(channel=self.channel_type, success=True)

        text = self._format_reply(envelope)
        if not text:
            return DeliveryReceipt(channel=self.channel_type, success=True)

        cards: list[card_actions.Card] = []
        if self._cards is not None and envelope.metadata.get("cards"):
            text, cards = card_actions.shown(text, envelope.metadata["cards"])
        receipt = DeliveryReceipt(channel=self.channel_type, success=True)
        if text and corr_id in self._group_message:
            receipt = await self._send_reply(corr_id, text, **receipt_kwargs)
        elif text:
            receipt = await self._send_message(chat_id, text, **receipt_kwargs)
        for card in cards:
            sent = await self._answer_card(corr_id, chat_id, card)
            if not text:
                receipt = sent
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

    async def _metrics_id(self, send: Awaitable[str | None]) -> str | None:
        try:
            return await send
        except LarkCliOutputError:
            # The command succeeded: an unreadable receipt must not trigger a resend.
            logger.warning("%s: sent a message without a readable receipt", self.channel_id)
            return None

    async def _send_reply(self, correlation_id: str, text: str, *, return_id: bool = False) -> DeliveryReceipt:
        """Answer a group message with a Markdown reply to it."""
        try:
            send = self._answer(correlation_id, markdown=text, **({"return_id": True} if return_id else {}))
            message_id = await self._metrics_id(send) if return_id else await send
        except LarkCliError as exc:
            logger.warning("%s: a reply in a group chat failed: %s", self.channel_id, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc),
                                   outcome=(PostOutcome.REJECTED if exc.refused
                                            else PostOutcome.UNKNOWN))
        return DeliveryReceipt(channel=self.channel_type, success=True, external_id=message_id)

    async def _answer_card(self, correlation_id: str, chat_id: str, card: card_actions.Card) -> DeliveryReceipt:
        """Send a card a reply carries where the reply went (under the group message it answers); its text alone
        when the card cannot be sent, so the user can still answer it by typing."""
        assert self._cards is not None
        content = card_actions.lark_card(replace(card, text=files.redact(card.text, self._runtime_root)))
        message_id = self._group_message.get(correlation_id)
        try:
            if message_id is not None:
                sent = await self._cards.reply(message_id, content, reply_in_thread=message_id in self._thread_messages)
            else:
                sent = await self._cards.send(content, chat_id=chat_id)
        except LarkCliError as exc:
            logger.warning("%s: a card failed, sending its text: %s", self.channel_id, exc)
            if message_id is not None:
                return await self._send_reply(correlation_id, card.text)
            return await self._send_message(chat_id, card.text)
        await card_actions.bind(card, *sent)
        return DeliveryReceipt(channel=self.channel_type, success=True)

    def _admits(self, sender: object, decision: RouteDecision | None, *, group: bool,
                log_refusal: bool = True) -> bool:
        """The admission rule of a message: a group bound explicitly with ``sender_policy`` ``chat`` admits everyone
        in it; else the bot's allowlist or the binding's extra senders."""
        chat_policy = group and decision is not None and decision.explicit and decision.sender_policy == "chat"
        return chat_policy or self._allowlist.admits(
            sender, chat_senders=decision.senders if decision is not None else frozenset(), log_refusal=log_refusal)

    async def _card_action(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Answer a press of a card's button on the long connection (``card_actions.respond``): a toast and the card
        as it is now, in time; a card whose action ends later is replaced then. None (the plain acknowledgement, as
        before cards) while card buttons are off. A press is admitted as a message in that chat would be (a card
        is in the chat it was sent to; in a direct chat only the user can press), and the card's own allowed
        confirmers narrow it further."""
        started = asyncio.get_running_loop().time()
        try:
            if not await asyncio.wait_for(card_actions.enabled(), card_actions.AUTHORIZE_SECONDS):
                return None
        except Exception:
            logger.warning("%s: cannot tell whether card buttons are on; a press is not answered", self.channel_id,
                           exc_info=True)
            return None
        presser, chat = str(event.get("operator_id") or ""), str(event.get("chat_id") or "")
        decision = self._resolve_chat(self.channel_id, chat) if self._resolve_chat is not None and chat else None
        if not self._admits(presser, decision, group=True, log_refusal=False):
            logger.info("%s: a card press by an unlisted sender %s was refused", self.channel_id, presser)
            return card_actions.lark_answer(card_actions.Answer(card_actions.NOT_ALLOWED["confirmation"], "error"))
        message_id = str(event.get("message_id") or "")
        cards = self._cards

        async def direct() -> bool:
            # Whether the press's chat is a direct chat (with the presser, then: a press comes from the bot's chats).
            mode = getattr(self._transport, "chat_mode", None)
            if mode is None or not chat:
                return False
            try:
                return await mode(chat) == "p2p"
            except LarkCliError as exc:
                logger.info("%s: cannot tell the mode of chat %s: %s", self.channel_id, chat, exc)
                return False

        async def update(card: card_actions.Card) -> None:
            if message_id and cards is not None:
                await cards.update(message_id, card_actions.lark_card(card))

        answer = await card_actions.respond(card_actions.Press(self.channel_id, presser, str(event.get("value") or ""),
                                                               chat=chat, message_id=message_id, direct=direct),
                                            update, started=started)
        card_actions.follow(answer, update)  # sent after this answer's card unless the action outlasts its 3 s
        return card_actions.lark_answer(answer)

    async def post_card(self, target: NotifyTarget, card: Any, *, reply_to: str | None = None,
                        idempotency_key: str | None = None) -> PostReceipt:
        """Post ``card`` with its buttons through the bot token's transport (``post`` otherwise): a reply to
        ``reply_to`` when given, else to the user or the group (the card @-mentions the requester)."""
        if self._cards is None:
            return await self.post(target, card.text, reply_to=reply_to, idempotency_key=idempotency_key)
        content = card_actions.lark_card(replace(card, text=files.redact(card.text, self._runtime_root)))
        try:
            if reply_to:
                message_id, chat = await self._cards.reply(reply_to, content, idempotency_key=idempotency_key,
                                                           reply_in_thread=reply_to in self._thread_messages)
            elif target.kind is TargetKind.USER:
                message_id, chat = await self._cards.send(content, user_id=target.id, idempotency_key=idempotency_key)
            else:
                message_id, chat = await self._cards.send(content, chat_id=target.id, idempotency_key=idempotency_key)
        except LarkCliError as exc:
            logger.warning("%s: a card post to %s failed: %s%s", self.channel_id, target.id, type(exc).__name__,
                           f" (code {exc.code})" if exc.code is not None else "")
            return PostReceipt(False, error=str(exc), parent_gone=bool(reply_to) and exc.code in MESSAGE_GONE_CODES,
                               outcome=PostOutcome.REJECTED if exc.refused else PostOutcome.UNKNOWN)
        await card_actions.bind(card, message_id, chat)
        return PostReceipt(True, external_ids=(message_id,) if message_id else ())

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

    async def _send_notice(self, target: NotifyTarget, text: str, *, return_id: bool = False) -> DeliveryReceipt:
        """Send a task's end notice as Markdown, like an answer: to a user in a direct message, or to a group chat
        @-mentioning the requester."""
        try:
            # Rich-text posts render Markdown; keep the durable notice unchanged.
            rendered = re.sub(
                r'(^## Process record\n\n)((?:- (?:Roles|Reviews|Verification|Escalations): .*\n?)+)',
                lambda block: block[1] + re.sub(r'([\\`*_~\[\]<>])', r'\\\1', block[2]),
                text, flags=re.MULTILINE)
            # A group notice carries the whole summary: several messages in order, the first @-mentioning the
            # requester. A part that fails ends the notice; the parts before it are not sent again.
            parts = split_message(rendered) if target.group else [rendered]
            for number, part in enumerate(parts):
                send = self._send_target(target if number == 0 else replace(target, mention=None), part,
                                         **({"return_id": True} if return_id else {}))
                message_id = await self._metrics_id(send) if return_id else await send
        except LarkCliError as exc:
            logger.warning("%s: a task's end notice to %s failed%s: %s", self.channel_id, target.id,
                           f" at part {number + 1} of {len(parts)}" if len(parts) > 1 else "", exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc),
                                   outcome=(PostOutcome.REJECTED if exc.refused
                                            else PostOutcome.UNKNOWN))
        return DeliveryReceipt(channel=self.channel_type, success=True, external_id=message_id)

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

    async def _send_message(self, chat_id: str, text: str, *, return_id: bool = False) -> DeliveryReceipt:
        try:
            message_id = None
            if return_id:
                message_id = await self._metrics_id(self._transport.send(chat_id=chat_id, markdown=text, return_id=True))
            else:
                await self._send_lark_message(chat_id, text)
            return DeliveryReceipt(channel=self.channel_type, success=True, external_id=message_id)
        except Exception as exc:
            logger.warning("%s delivery failed: %s", self.channel_id, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc),
                                   outcome=(PostOutcome.REJECTED if isinstance(exc, LarkCliError) and exc.refused
                                            else PostOutcome.UNKNOWN))

    async def _send_status(self, correlation_id: str, text: str) -> str | None:
        with self._transport.message_scope(correlation_id, durable=False):
            return await self._answer(correlation_id, text=text, return_id=True)

    async def _edit_status(self, chat_id: str, message_id: str, text: str) -> None:
        await self._transport.edit(message_id, text=text)

    async def _react(self, chat_id: str, message_id: str) -> None:
        await self._transport.react(message_id)

    async def notify(self, chat: str, text: str, *, idempotency_key: str | None = None) -> DeliveryReceipt:
        with self._transport.message_scope(idempotency_key or str(_uuid.uuid4()), source="notify", chat=chat):
            return await self._notify(chat, text)

    async def _notify(self, chat: str, text: str) -> DeliveryReceipt:
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
            send = self._transport.send(
                **destination, text=text, **({"return_id": True} if chat_settings.metrics_enabled() else {}))
            message_id = await self._metrics_id(send) if chat_settings.metrics_enabled() else await send
        except LarkCliError as exc:
            logger.warning("%s notification to %s failed: %s", self.channel_id, chat, exc)
            return DeliveryReceipt(channel=self.channel_type, success=False, error=str(exc),
                                   outcome=(PostOutcome.REJECTED if exc.refused
                                            else PostOutcome.UNKNOWN))
        return DeliveryReceipt(channel=self.channel_type, success=True, external_id=message_id)

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        with self._transport.message_scope(str(_uuid.uuid4()), source="post", chat=target.id):
            return await self._post(target, text, reply_to=reply_to, idempotency_key=idempotency_key)

    async def _post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
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
            # The error's text (lark-cli's own words, and whatever it printed) is in the receipt, not in the log.
            # Receipt parsing has a distinct type for metrics; keep the existing failed-post log classification.
            error_type = LarkCliError.__name__ if isinstance(exc, LarkCliOutputError) else type(exc).__name__
            logger.warning("%s: a post to %s failed: %s%s", self.channel_id, target.id, error_type,
                           f" (code {exc.code})" if exc.code is not None else "")
            return PostReceipt(False, error=str(exc), parent_gone=bool(reply_to) and exc.code in MESSAGE_GONE_CODES,
                               outcome=PostOutcome.REJECTED if exc.refused else PostOutcome.UNKNOWN)
        # Only a successful answer (lark-cli ``ok: true``, the Bot API's HTTP 200 with ``code`` 0) gets here: the message
        # was sent, and replies to it just cannot be routed when its id was not given.
        return PostReceipt(True, external_ids=(message_id,) if message_id else ())

    async def edit_card(self, target: NotifyTarget, external_id: str, card: Any) -> PostReceipt:
        """Update an interactive card through the same guarded writer as a callback's update."""
        if self._cards is None:
            return await self.edit(target, external_id, card.text)
        content = card_actions.lark_card(replace(card, text=files.redact(card.text, self._runtime_root)))
        try:
            await self._cards.update(external_id, content)
        except LarkCliError as exc:
            return PostReceipt(False, error=str(exc),
                               outcome=PostOutcome.REJECTED if exc.refused else PostOutcome.UNKNOWN)
        return PostReceipt(True, external_ids=(external_id,))

    async def edit(self, target: NotifyTarget, external_id: str, text: str) -> PostReceipt:
        getattr(self, "_quote_deliveries", {}).pop(external_id, None)  # even an uncertain edit invalidates the stored copy
        text = files.redact(text, self._runtime_root)
        try:
            await self._transport.edit(external_id, markdown=text)
        except LarkCliError as exc:
            return PostReceipt(False, error=str(exc),
                               outcome=PostOutcome.REJECTED if exc.refused else PostOutcome.UNKNOWN)
        return PostReceipt(True, external_ids=(external_id,))

    def healthcheck(self) -> dict[str, Any]:
        info = {
            "channel": self.channel_type,
            "healthy": self._running,
            "active_conversations": len(self._correlation_chat),
        }

        if self.channel_type == "lark":
            from ..attention import DELIVERY_WINDOW

            windows = [window for window in self._catchup_status["windows"]
                       if window["state"] != "dropped"
                       or window["recorded_at"] >= time() - DELIVERY_WINDOW.total_seconds()]
            info["catchup"] = {"pending": self._catchup_status["pending"],
                               "dropped": sum(w["state"] == "dropped" for w in windows), "windows": windows}
        if self._transport.kind is LarkTransportKind.CLI:
            info["lark_cli_available"] = self._transport.available()
        else:
            info["transport"] = type(self._transport.inner).__name__
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
                raise _HistoryFailure(CatchupDropReason.PAGINATION)
            token = next_token
        raise _HistoryFailure(CatchupDropReason.PAGE_CAP)

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
                if created.before(start) or (bots_only and created.before(time() - _CATCHUP_MAX_AGE)):
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
                        await self._inbox.drop_window(chat_id, start, end, CatchupDropReason.PAGE_CAP)
                        break
                    next_token = page.get("page_token")
                    if not next_token or next_token == token:
                        raise _HistoryFailure(CatchupDropReason.PAGINATION)
                    token = next_token
            except Exception as exc:
                terminal = _terminal_history_failure(exc)
                if terminal is not None:
                    await self._inbox.drop_window(chat_id, start, end, terminal)
                else:
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
        assert self._inbox is not None
        if not bots_only:
            await self._inbox.defer_thread(thread_id, chat_id, start, end)
        failure = None
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
        except Exception as exc:
            failure = exc
            stats["thread_failures"] += 1
            logger.warning("%s: catch-up thread failed chat=%s thread=%s", self.channel_id, chat_id,
                           thread_id, exc_info=True)
        # Admit already-read pages even when a later page fails, oldest first within the thread.
        for page in reversed(pages):
            await self._catchup_messages(
                {**page, "messages": [{**message, "thread_id": thread_id}
                                      for message in reversed(page["messages"])]}, chat_id,
                start=start, end=end, stats=stats, bots_only=bots_only)

        if not bots_only:
            await self._inbox.settle_thread(thread_id, _CATCHUP_MAX_ATTEMPTS,
                                             _terminal_history_failure(failure) if failure else None,
                                             failed=failure is not None)

    async def _startup_catchup(self, boundary: float | None) -> None:
        await self._catch_up(boundary)
        if self._running and self._catchup_status["pending"]:
            self._catchup_retry_task = asyncio.create_task(self._retry_catchup())

    async def _retry_catchup(self) -> None:
        retry = 0
        while self._running and self._catchup_status["pending"]:
            delay = _CATCHUP_RETRY_DELAYS[min(retry, len(_CATCHUP_RETRY_DELAYS) - 1)]
            await self._catchup_retry_wait(delay)
            if not self._running:
                return
            # No new discovery or live window: only the durable work still owed.
            await self._catch_up(None)
            retry += 1

    async def _catch_up(self, boundary: float | None) -> None:
        stats = dict(chats_scanned=0, messages_found=0, handled=0, duplicates=0, too_old=0, failures=0,
                     threads_scanned=0, thread_failures=0, thread_roots_capped=0)
        expired = boundary is not None and time() - boundary > _CATCHUP_MAX_AGE
        discovering = False
        waiting_for_consumer = False
        try:
            assert self._inbox is not None
            if boundary is not None:
                await self._inbox.defer_discovery(boundary - _CATCHUP_OVERLAP, time())
            await self._inbox.clamp_catchup(time() - _CATCHUP_MAX_AGE,
                CatchupDropReason.BOUNDARY_EXPIRED if expired else CatchupDropReason.STALE)
            debts = await self._inbox.debts()
            discovery = await self._inbox.discovery()
            live_start = None
            live_end = time()
            if discovery is not None:
                live_start = discovery[0]
                live_end = max(live_end, discovery[1]) if boundary is not None else discovery[1]
            thread_debts = await self._inbox.thread_debts()
            if (live_start is None and not debts and not thread_debts) or not hasattr(self._transport, "list_messages"):
                return
            discovering = live_start is not None
            chats = await self._catchup_chats() if discovering else []
            # Persist all planned work before awaiting history: live checkpoints can
            # advance independently, even if startup is cancelled halfway through.
            windows = dict(debts)
            if live_start is not None:
                for chat_id in dict.fromkeys([*debts, *chats]):
                    owed = debts.get(chat_id)
                    windows[chat_id] = (min(live_start, owed[0]) if owed else live_start,
                                        max(live_end, owed[1]) if owed else live_end)
            for chat_id, (start, end) in windows.items():
                await self._inbox.defer(chat_id, start, end)
            if ready := getattr(self._transport, "consumer_started", None):
                waiting_for_consumer = True
                await asyncio.wait_for(ready.wait(), timeout=60)
                waiting_for_consumer = False
            if discovering:
                # Include messages arriving while the live consumer was becoming ready.
                end = time()
                for chat_id, (start, previous_end) in windows.items():
                    windows[chat_id] = (start, max(end, previous_end))
                    await self._inbox.defer(chat_id, *windows[chat_id])
                await self._inbox.complete_discovery()
            discovering = False
            seen_threads: set[str] = set()
            for chat_id, (start, end) in list(windows.items())[:_CATCHUP_MAX_CHATS]:
                stats["chats_scanned"] += 1
                failures = stats["failures"]
                terminal = None
                try:
                    async for page in self._history_pages(
                        self._transport.list_messages, chat_id, start=start, end=end,  # type: ignore[attr-defined]
                    ):
                        await self._catchup_messages(page, chat_id, start=start, end=end, stats=stats)
                    await self._catchup_threads(chat_id, start=start, end=end, stats=stats, seen=seen_threads)
                    if failures == stats["failures"]:
                        await self._inbox.complete(chat_id)
                        continue
                except Exception as exc:
                    terminal = _terminal_history_failure(exc)
                    stats["failures"] += 1
                    logger.warning("%s: catch-up chat failed chat=%s", self.channel_id, chat_id, exc_info=True)
                await self._inbox.fail_catchup(chat_id, _CATCHUP_MAX_ATTEMPTS, terminal)
            for thread_id, chat_id, start, end in thread_debts:
                await self._catchup_thread(thread_id, chat_id, start=start, end=end, stats=stats, seen=seen_threads)
        except Exception as exc:
            stats["failures"] += 1
            logger.warning("%s: catch-up discovery failed", self.channel_id, exc_info=True)
            if self._inbox is not None:
                if discovering:
                    await self._inbox.fail_catchup(None, _CATCHUP_MAX_ATTEMPTS, _terminal_history_failure(exc))
                if waiting_for_consumer:
                    for chat_id in await self._inbox.debts():
                        await self._inbox.fail_catchup(chat_id, _CATCHUP_MAX_ATTEMPTS)
        finally:
            if self._inbox is not None:
                self._catchup_status = await self._inbox.catchup_status()
            logger.info("lark_catchup channel=%s boundary=%s boundary_expired=%s %s pending=%s dropped=%s",
                        self.channel_id, boundary, expired, " ".join(f"{key}={value}" for key, value in stats.items()),
                        self._catchup_status["pending"], self._catchup_status["dropped"])

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
                if not stats["failures"] and not stats["thread_failures"]:
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
                    and not self._ingress_lock.locked() and ready is not None and ready.is_set()):
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
            if self._inbox is not None and message_id and await self._inbox.processed(message_id):
                return "duplicate"
            created = event.get("_catchup_created_at")
            if (self._inbox is not None and event.get("type") == RECEIVE_EVENT
                    and created is None and event.get("sender_type") in {"app", "bot"} and event.get("chat_id")):
                await self._inbox.remember_bot(event["chat_id"], time())
            if self._own_message(event):
                event["_bot_outcome"] = "own_message"
                handled = False
            elif event.get("chat_type") == "group" and not self._bot_mentions(event):
                handled = False
            elif created is not None and created.before(time() - _CATCHUP_MAX_AGE):
                if self._inbox is not None:
                    await self._inbox.drop_window(event.get("chat_id") or "", created.at, created.at,
                                                  CatchupDropReason.STALE)
                    if message_id:
                        await self._inbox.finish(message_id)
                return "too_old"
            else:
                handled = await self._process_lark_event(event)
            if event.get("sender_type") in {"app", "bot"}:
                logger.info("lark_bot_message chat=%s bot_id=%s message_id=%s outcome=%s",
                            event.get("chat_id"), event.get("sender_id"), message_id,
                            "handled" if handled else event.get("_bot_outcome", "ignored"))
            if self._inbox is not None and message_id and event.get("type") == RECEIVE_EVENT:
                await self._inbox.finish(message_id, chat_id=event.get("chat_id") or "",
                                   thread_id=(event.get("thread_id") or "") if handled else "", at=time())
            return "handled" if handled else "ignored"

    async def _process_lark_event(self, event: dict[str, Any]) -> bool | None:
        with self._transport.message_scope(event.get("message_id") or str(_uuid.uuid4()),
                                           chat=event.get("chat_id")):
            return await self._process_scoped_event(event)

    async def _process_scoped_event(self, event: dict[str, Any]) -> bool | None:
        """Publish text, rich posts and merged forwards to the Bus, with a reference to what they quote
        (``payload["quote_ref"]``, read by the Manager), and answer other admitted message types. The event is
        lark-cli's flat one: ``chat_id``,
        ``chat_type`` (``p2p`` or ``group``), ``message_id``, ``message_type``, ``sender_id`` (the open_id),
        ``sender_type``, ``mentions`` (each ``id``, an open_id, ``key`` and ``name``), ``content``, a text
        message's text with each mention shown as ``@name``, for a reply ``reply_to`` and ``root_id``, and in a
        thread ``thread_id``."""
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
        # The bot stream already decided admission before queueing; CLI events decide here.
        admitted = event.get("admitted")
        decision = (self._resolve_chat(self.channel_id, chat_id)
                    if admitted is not False and self._resolve_chat is not None else None)
        if not group and decision is not None:
            decision = replace(decision, sender_policy=SenderPolicy.LISTED)
        bot = event.get("sender_type") in {"app", "bot"}
        log_refusal = True
        if bot:
            now = monotonic()
            self._bot_refusals = {key: at for key, at in self._bot_refusals.items()
                                  if now - at < _BOT_WINDOW_SECONDS}
            log_refusal = (chat_id, str(sender_open_id)) not in self._bot_refusals
        if admitted is False and log_refusal:
            logger.warning("%s: dropped a message from sender %r, refused by the stream prefilter",
                           self.channel_id, str(sender_open_id or "<unknown>")[:80])
        if admitted is False or (admitted is not True and not self._admits(sender_open_id, decision, group=group,
                                                                           log_refusal=log_refusal)):
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
        stop = is_stop_command(text)
        if await self._turned_off(chat_id, message_id if group else None, manager_id=manager_id,
                                  explicit=explicit, stop=stop):
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
        attachments = await self._collect(message_id, inbound.resources) if inbound.resources else []
        if any(item.state is not AttachmentState.SAVED for item in attachments):
            try:
                reply = _not_received(attachments)
                if group:
                    await self._reply(message_id, text=reply)
                else:
                    await self._transport.send(chat_id=chat_id, text=reply)
                attachments = [replace(item, told=True) if item.state is not AttachmentState.SAVED else item
                               for item in attachments]
            except LarkCliError:
                logger.warning("%s: could not report attachments not received", self.channel_id, exc_info=True)
        correlation_id = str(_uuid.uuid4())
        self._correlation_chat[correlation_id] = chat_id
        context = {"chat_id": chat_id, "chat_type": "group" if group else "p2p", "sender_id": sender_open_id or "",
                   "message_id": message_id}
        # What the message replies to: the message it answers and the first of its thread (a gate's messages).
        for key in ("reply_to", "root_id", "thread_id"):
            if isinstance(ref := event.get(key), str) and ref:
                context[key] = ref
        if group:
            if message_id:
                self._group_message[correlation_id] = message_id
            context["sender_name"] = await self._sender_name(sender_open_id or "")
        # What it quotes is read by the Manager, as it prepares the turn (``read_quote``); a /stop reads nothing.
        quote_ref = None if stop else QuoteRef.received(
            self.channel_id, event, forward=inbound.kind is lark_content.ContentKind.FORWARD)

        envelope = MessageEnvelope.create(
            source=self._address,
            target=manager_address(manager_id),
            message_type=MessageType.USER_MESSAGE,
            payload={
                "text": text,
                **({"attachments": [item.to_payload() for item in attachments]} if attachments else {}),
                **({"quote_ref": quote_ref.to_payload()} if quote_ref is not None else {}),
                "language": "zh",
                "channel_context": context,
            },
            correlation_id=correlation_id,
            metadata=metadata,
        )
        if created := event.get("_catchup_created_at"):
            envelope = replace(envelope, timestamp=datetime.fromtimestamp(created.at, timezone.utc).isoformat())
        await self._bus.publish(envelope)
        return True

    async def read_quote(self, ref: QuoteRef, *, after: int) -> QuoteRead:
        """What a message received here quotes (``QuoteRef.targets``), read as the bot within ``_QUOTE_SECONDS``,
        names included; a read that fails becomes a line saying why. The images and files in it are saved beside
        the message's own, numbered after its ``after`` and within its limits (``_collect``). The Manager calls it
        while it prepares the message's turn (``lark_quote.QuoteReader``)."""
        delivery = getattr(self, "_quote_deliveries", {}).get(ref.parent_id)
        if (ref.kind is QuoteKind.REPLY and not ref.thread_id and delivery is not None
                and ref.session is not None and delivery.session == ref.session and delivery.chat_id == ref.chat_id):
            quote, _ = lark_quote.build(QuoteKind.REPLY, ref.parent_id, [delivery.item],
                                        own=(self._bot_open_id, self._bot_app_id), after=after)
            return QuoteRead((quote,), decision=lark_quote.QuoteDecision.SKIP_OWN_IN_SESSION)
        read = self._transport.get_message  # type: ignore[attr-defined]
        wanted = ref.targets()
        started = monotonic()
        deadline = started + _QUOTE_SECONDS

        async def fetch(kind: QuoteKind, target: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
            try:
                async with asyncio.timeout(deadline - monotonic()):
                    if kind is not QuoteKind.THREAD:
                        return await read(target, timeout=deadline - monotonic()), [], ""
                    root, page = await asyncio.gather(
                        read(target, timeout=deadline - monotonic()),
                        self._transport.list_thread_messages(ref.thread_id),  # type: ignore[attr-defined]
                        return_exceptions=True)
                    if isinstance(root, BaseException):
                        raise root
                    if isinstance(page, BaseException):
                        if target != ref.parent_id or not isinstance(page, (TimeoutError, LarkCliError)):
                            raise page
                        # It replies to the first message, which is read: that alone, as a reply.
                        logger.warning("%s: could not list thread %s: %s", self.channel_id, ref.thread_id,
                                       lark_quote.failure(page))
                        return root, [], ""
                    shown = {ref.message_id, target, ref.parent_id}
                    replies = [item for item in page["messages"] if item.get("message_id") not in shown]
                    return root, replies[:lark_quote.THREAD_TAIL][::-1], ""
            except (TimeoutError, LarkCliError) as exc:
                return [], [], lark_quote.failure(exc)
            except Exception:
                logger.warning("%s: could not read quoted message %s", self.channel_id, target, exc_info=True)
                return [], [], "error"

        read_all = await asyncio.gather(*(fetch(kind, target) for kind, target in wanted))
        read_at = monotonic()
        names: dict[str, str] = {}
        if ids := lark_quote.sender_ids(item for items, thread, _ in read_all for item in (*items, *thread)):
            try:
                names = await asyncio.wait_for(self._transport.names(ids),
                                               max(min(_NAME_LOOKUP_SECONDS, deadline - monotonic()), 0.1))
            except (TimeoutError, LarkCliError):
                names = {}
        named_at = monotonic()
        quotes: list[lark_quote.Quote] = []
        resources: list[lark_content.ResourceRef] = []
        for (kind, target), (items, thread, error) in zip(wanted, read_all):
            if kind is QuoteKind.THREAD and target == ref.parent_id and not thread:
                kind = QuoteKind.REPLY  # it replies to the first message, and nothing else in the thread is shown
            quote, found = lark_quote.build(kind, target, items, thread=thread, names=names, error=error,
                                            own=(self._bot_open_id, self._bot_app_id), after=after + len(resources))
            quotes.append(quote)
            resources += found
        # Beside the message's own, which stay (they are numbered before ``after``); read again on a replay.
        attachments = await self._collect(ref.message_id, tuple(resources), keep=True) if resources else []
        return QuoteRead(tuple(quotes), tuple(attachments), read_seconds=read_at - started,
                         names_seconds=named_at - read_at, files_seconds=monotonic() - named_at)

    async def _collect(self, message_id: str, refs: tuple[lark_content.ResourceRef, ...], *,
                       keep: bool = False) -> list[Attachment]:
        """Download ``refs`` into the message's attachment directory; ``keep``: add to what is there."""
        deadline = monotonic() + _DOWNLOAD_MESSAGE_SECONDS
        try:
            directory = files.message_dir(self._runtime_root, self.channel_id, message_id, keep=keep)
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
                    part.unlink(missing_ok=True)  # a kept directory may hold one a stopped earlier read left
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
                receipt = await self.deliver(envelope)
                self.record_envelope_delivery(envelope, receipt)
                if chat_settings.metrics_enabled() and envelope.metadata.get("proactive"):
                    route = envelope.metadata.get("reply_route")
                    route = route if isinstance(route, dict) else {}
                    chat = str(route.get("chat_id") or self._correlation_chat.get(envelope.correlation_id) or "")
                    group = envelope.correlation_id in self._group_message or bool(route.get("reply_to"))
                    record_delivery(self.channel_id, envelope, receipt, chat=chat, group=group)
                await record_reply(self.channel_type, envelope, receipt,
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
