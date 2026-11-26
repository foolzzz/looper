"""Telegram Bot Channel Adapter.

Bridges Telegram Bot API with the agent Bus. Uses long-polling by default
(webhook mode planned). ``looper serve`` builds this adapter from the
``channel-telegram`` row that the dashboard Settings page edits.

Only the senders on its allowlist reach the Manager (``allowlist``): anyone
else is dropped with one log line and gets no reply, and an empty list keeps
the adapter from starting. A direct message that reaches the Manager first
gets the bot's 👀 reaction (``status_line.Reactions``).

A user gate's messages are posted directly (``post``: plain text to the chat,
optionally as a reply to a message, returning the id of every message sent), and
a forwarded message's own ``message_id`` and the ``reply_to_message_id`` of the
message it replies to reach the Manager in ``channel_context``.

Fallback when the constructor's token is empty:
  LOOPER_TELEGRAM_BOT_TOKEN — Bot token from @BotFather
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from ... import env
from ..bus import MessageBus
from ..log_redaction import add_secret, redact
from ..message import (
    MessageEnvelope,
    MessageType,
    channel_address,
    channel_thread_id,
    manager_address,
    parse_channel_id,
)
from ..task_notice import Notice, NotifyTarget, TargetKind, split_message
from .. import task_notice_delivery
from ..ops.leads import for_group
from .allowlist import SenderAllowlist
from .. import card_actions, chat_attachments, chat_commands, chat_settings
from .base import ChannelAdapter, DeliveryFailure, DeliveryReceipt, PostOutcome, PostReceipt
from .chat_history import record_reply
from .proactive import record_delivery, store_envelope_body
from .chat_routing import ChatResolver, RouteAction, RouteDecision, observe_chat, log_route
from .status_line import Reactions, StatusKind, StatusLines, status_kind
from .telegram_inbox import TelegramInbox
from .outbound import Gateway, Outbox, OutboundProtocol, OutboxSource, derived_key
from .. import chat_settings

logger = logging.getLogger(__name__)

# Cap the correlation→chat_id map so a long-lived bot does not grow without bound; the
# oldest entries are evicted first. Autonomous replies reuse recent correlations, so losing
# the oldest is safe. Matches the web channel's bound.
_CORRELATION_MAX = 1000

_BOT_COMMANDS = chat_commands.COMMANDS


def bot_commands() -> list[tuple[str, str]]:
    """The command menu (setMyCommands), from the list every channel's /help shares."""
    return chat_commands.available()


def _check_telegram_available() -> bool:
    try:
        import telegram  # noqa: F401
        return True
    except ImportError:
        return False


def _post_outcome(exc: Exception) -> PostOutcome:
    """Whether Telegram refused a send (its answer said no: nothing was sent) or the answer was lost (a time-out or a
    broken connection after the request went out: the message may be there)."""
    from telegram.error import BadRequest, ChatMigrated, Forbidden, InvalidToken, RetryAfter, TimedOut
    from .telegram_request import TelegramWriteError
    from .outbound import ErrorClass

    if isinstance(exc, TelegramWriteError):
        return PostOutcome.REJECTED if exc.error_class == ErrorClass.REJECTED else PostOutcome.UNKNOWN
    if isinstance(exc, TimedOut):
        return PostOutcome.UNKNOWN
    if isinstance(exc, (BadRequest, Forbidden, ChatMigrated, InvalidToken, RetryAfter)):
        return PostOutcome.REJECTED
    return PostOutcome.UNKNOWN


class TelegramChannelAdapter(ChannelAdapter):
    """Bridges Telegram Bot with the agent message bus."""

    def __init__(
        self,
        *,
        allowlist: SenderAllowlist,
        bot_token: str | None = None,
        channel_id: str | None = None,
        manager_id: str = "manager",
        manager_running: Callable[[str], bool] | None = None,
        resolve_chat: ChatResolver | None = None,
        stop_chat: Callable[..., Awaitable[Any]] | None = None,
        backlog_max_age_seconds: int = 600,
    ) -> None:
        if channel_id is not None:
            parsed = parse_channel_id(channel_id)
            if parsed is None or parsed.channel_type != "telegram":
                raise ValueError(f"not a Telegram channel id: {channel_id!r}")
        self._channel_id = channel_id or "channel-telegram"
        self._manager_id = manager_id
        self._manager_running = manager_running
        self._resolve_chat = resolve_chat
        self._stop_chat = stop_chat
        if type(backlog_max_age_seconds) is not int or backlog_max_age_seconds <= 0:
            raise ValueError("backlog_max_age_seconds must be a positive integer")
        self._backlog_max_age_seconds = backlog_max_age_seconds
        self._inbox: TelegramInbox | None = None
        self._stop_boundary: Awaitable[None] | None = None
        self._reached_publish: set[int] = set()
        # Only the built-in row falls back to the environment; an added bot has its own token.
        self._bot_token = bot_token or (env.get("LOOPER_TELEGRAM_BOT_TOKEN", "") if channel_id is None else "")
        self._allowlist = allowlist  # Telegram user ids, as strings
        self._bus: MessageBus | None = None
        self._address = channel_address("telegram", self._channel_id)
        self._app: Any = None
        self._requests: tuple[Any, ...] = ()
        self._gateway: Gateway | None = None
        self._channel_outbox: Outbox | None = None
        self._outbound_task: asyncio.Task[Any] | None = None
        self._running = False
        # correlation_id → telegram chat_id for reply routing (bounded, oldest evicted first)
        self._correlation_chat: OrderedDict[str, int] = OrderedDict()
        # One status message per message answered, edited in place (``status_line``).
        self._status = StatusLines(channel=self._channel_id, send=self._send_status, edit=self._edit_status)
        self._reactions = Reactions(channel=self._channel_id, react=self._react)

    @property
    def channel_type(self) -> str:
        return "telegram"

    @property
    def channel_id(self) -> str:
        return self._channel_id

    async def start(self, bus: MessageBus) -> None:
        if not self._allowlist.ids:
            # The Manager runs any command it is asked to: nobody may reach it unlisted.
            logger.error("%s not started: its sender allowlist is empty", self._channel_id)
            return

        if not _check_telegram_available():
            logger.error(
                "python-telegram-bot not installed; it is a default Looper "
                "dependency, so reinstall Looper (for a checkout: uv sync)"
            )
            return

        if not self._bot_token:
            logger.error("%s not started: no bot token (LOOPER_TELEGRAM_BOT_TOKEN for the built-in row)", self._channel_id)
            return

        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            TypeHandler,
            filters,
        )
        from telegram import Update
        from telegram.request import HTTPXRequest
        from .telegram_request import GatedRequest, TelegramOutboxTransport

        self._bus = bus
        self._running = True

        add_secret(self._bot_token)  # python-telegram-bot puts it in every request URL and in the Bot's repr
        try:
            builder = Application.builder().token(self._bot_token)
            # update_id is unique within a bot, not across bots assigned to the same
            # channel row. Rotating one bot's token keeps its numeric identity.
            identity = self._bot_token.partition(":")[0]
            bot_id = identity if identity.isdecimal() else "invalid"
            self._inbox = await TelegramInbox.open(bus.runtime_root / ".local" / "telegram" / self.channel_id / f"{bot_id}.sqlite3")
            inbox = self._inbox

            class PollingRequest(HTTPXRequest):
                async def do_request(self, url: str, **kwargs: Any) -> tuple[int, bytes]:
                    code, payload = await super().do_request(url=url, **kwargs)
                    if code == 200 and url.endswith("/getUpdates"):
                        response = json.loads(payload)
                        if response.get("ok"):
                            await inbox.polling_received(response["result"])
                    return code, payload

            # Own both transports even if Application.initialize fails. Save polling
            # responses before PTB acknowledges them, retaining its network retry loop.
            self._gateway = Gateway(self.channel_id, app_id=f'telegram:{identity}')
            self._channel_outbox = Outbox(
                self.channel_id, TelegramOutboxTransport(lambda: self._app.bot), self._gateway,
                protocol=OutboundProtocol.TELEGRAM, enabled=chat_settings.outbound_enabled)
            self._requests = (PollingRequest(connection_pool_size=1), GatedRequest(
                self._gateway, enabled=chat_settings.outbound_enabled, connection_pool_size=256))
            builder.get_updates_request(self._requests[0])
            builder.request(self._requests[1])
            builder.update_queue(self._inbox)
            self._app = builder.build()

            self._app.add_handler(CommandHandler("start", self._durable(self._cmd_start)))
            self._app.add_handler(CommandHandler("status", self._durable(self._cmd_status)))
            self._app.add_handler(CommandHandler("help", self._durable(self._cmd_help)))
            # Only /new itself (also /new@<this bot>): "/new foo" stays an unknown command, as before.
            self._app.add_handler(CommandHandler("new", self._durable(self._cmd_new), has_args=False))
            self._app.add_handler(CommandHandler("stop", self._durable(self._cmd_stop), has_args=False))
            # The gate commands take arguments; the Manager parses and answers them in code, so the text goes as typed.
            self._app.add_handler(CommandHandler("gates", self._durable(self._cmd_gates)))
            self._app.add_handler(CommandHandler("gate", self._durable(self._cmd_gate)))
            self._app.add_handler(CommandHandler("profile", self._durable(self._cmd_profile)))
            self._app.add_handler(CommandHandler("todo", self._durable(self._cmd_todo), has_args=False))
            self._app.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._durable(self._on_message))
            )
            self._app.add_handler(CallbackQueryHandler(self._durable(self._on_button)))
            # The first matching handler wins in this group. Unhandled updates still
            # need retiring from the durable inbox (photos, unknown commands, etc.).
            self._app.add_handler(TypeHandler(Update, self._durable(self._ignore_update)))

            self._outbound_task = asyncio.create_task(self._outbound_loop())

            await self._app.initialize()
            await self._channel_outbox.start_if_needed()
            await self._inbox.polling_started(int(datetime.now(timezone.utc).timestamp()))
            await self._inbox.replay(self._app.bot)
            try:
                from telegram import BotCommand

                await self._app.bot.set_my_commands(
                    [BotCommand(name, desc) for name, desc in bot_commands()]
                )
            except Exception:
                logger.warning("telegram set_my_commands failed", exc_info=True)
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=False)  # type: ignore[union-attr]
        except BaseException:
            await self.stop()
            raise
        logger.info("telegram channel adapter started")

    def prepare_stop(self) -> None:
        self._queue_stop_boundary()
        if self._inbox is not None and self._stop_boundary is not None:
            # The signal hook must not release the agent drain before commit.
            self._inbox.flush()

    def _queue_stop_boundary(self) -> None:
        if self._inbox is not None and self._app is not None and self._stop_boundary is None:
            updater = self._app.updater
            if updater is not None and updater.running:
                try:
                    self._stop_boundary = self._inbox.polling_stopped(int(datetime.now(timezone.utc).timestamp()))
                except Exception:
                    logger.warning("%s: telegram polling stop boundary could not be saved",
                                   self._channel_id, exc_info=True)

    async def stop(self) -> None:
        self._queue_stop_boundary()
        if self._stop_boundary is not None:
            try:
                await self._stop_boundary
            except Exception:
                logger.warning("%s: telegram polling stop boundary could not be saved", self._channel_id, exc_info=True)
        self._running = False

        async def cleanup(stage: str, action: Callable[[], Awaitable[Any]]) -> None:
            try:
                await action()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("%s: telegram %s cleanup failed", self._channel_id, stage, exc_info=True)

        if self._outbound_task:
            task = self._outbound_task
            task.cancel()
            await cleanup("outbound", lambda: task)
        if self._app:
            updater = self._app.updater
            if updater is not None and updater.running:
                await cleanup("updater.stop", updater.stop)
            if self._app.running:
                await cleanup("stop", self._app.stop)
        # Application.stop drains received updates, whose handlers can still add reactions.
        await cleanup("reactions", self._reactions.stop)
        if self._channel_outbox is not None:
            await cleanup("outbox", self._channel_outbox.stop)
        if self._gateway is not None:
            await cleanup("gateway", self._gateway.stop)
        self._channel_outbox = None
        self._gateway = None
        if self._app:
            await cleanup("shutdown", self._app.shutdown)
        for request in self._requests:
            await cleanup("request.shutdown", request.shutdown)
        self._app = None
        self._requests = ()
        self._outbound_task = None
        if self._inbox is not None:
            await self._inbox.close()
            self._inbox = None
        self._stop_boundary = None
        self._bus = None
        logger.info("telegram channel adapter stopped")

    async def notify(self, chat: str, text: str, *, idempotency_key: str | None = None) -> DeliveryReceipt:
        """Send ``text`` as plain text to Telegram chat ``chat`` (a user's private chat has the user's id)."""
        if self._app is None:
            return DeliveryReceipt(channel="telegram", success=False, error="the Telegram bot is not started")
        text = self._redact(text)
        if chat_settings.outbound_enabled():
            receipt = await self._outbox_messages(chat, text, key=idempotency_key, source=OutboxSource.NOTIFY)
            return self._delivery_receipt(receipt)
        try:
            for chunk in self._split_message(text, max_len=4000):
                msg = await self._app.bot.send_message(chat_id=int(chat), text=chunk)
            return DeliveryReceipt(channel="telegram", success=True, external_id=str(msg.message_id))
        except Exception as exc:
            logger.warning("telegram notification to %s failed: %s", chat, exc)
            return DeliveryReceipt(channel="telegram", success=False, error=redact(str(exc)), outcome=_post_outcome(exc))

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        """Send ``text`` as plain text to the Telegram chat ``target.id``, in messages of at most 4,000 characters,
        the first a reply to the message ``reply_to`` when given (sent on its own when that message is gone). The
        ids of the messages sent are kept when a later one fails, so replies to them still route. Telegram has no
        idempotency key: a post retried after a lost answer may be sent twice."""
        if self._app is None:
            return PostReceipt(False, error="the Telegram bot is not started", outcome=PostOutcome.REJECTED)
        try:
            chat = int(target.id)
        except ValueError:
            return PostReceipt(False, error=f"not a Telegram chat id: {target.id!r}", outcome=PostOutcome.REJECTED)
        try:
            reply = int(reply_to) if reply_to else None
        except ValueError:
            return PostReceipt(False, error=f"not a Telegram message id: {reply_to!r}", outcome=PostOutcome.REJECTED)
        sent: list[str] = []
        text = self._redact(text)
        if chat_settings.outbound_enabled():
            return await self._outbox_messages(str(chat), text, key=idempotency_key,
                                               reply_to=reply, source=OutboxSource.POST)
        try:
            for chunk in self._split_message(text, max_len=4000):
                extra = ({"reply_to_message_id": reply, "allow_sending_without_reply": True}
                         if reply is not None and not sent else {})
                message = await self._app.bot.send_message(chat_id=chat, text=chunk, **extra)
                sent.append(str(message.message_id))
        except Exception as exc:
            error = redact(str(exc))
            logger.warning("telegram: a post to %s failed (%d of its messages sent): %s", target.id, len(sent),
                           type(exc).__name__)
            return PostReceipt(False, external_ids=tuple(sent), error=error, outcome=_post_outcome(exc))
        return PostReceipt(True, external_ids=tuple(sent))

    async def post_card(self, target: NotifyTarget, card: Any, *, reply_to: str | None = None,
                        idempotency_key: str | None = None) -> PostReceipt:
        """Send ``card`` as one plain-text message with its buttons as an inline keyboard, else like ``post``."""
        if self._app is None:
            return PostReceipt(False, error="the Telegram bot is not started", outcome=PostOutcome.REJECTED)
        try:
            chat, reply = int(target.id), int(reply_to) if reply_to else None
        except ValueError:
            return PostReceipt(False, error="invalid Telegram chat or message id", outcome=PostOutcome.REJECTED)
        extra = {"reply_to_message_id": reply, "allow_sending_without_reply": True} if reply is not None else {}
        if chat_settings.outbound_enabled():
            assert self._channel_outbox is not None
            key = idempotency_key or str(uuid4())
            body = dict(chat_id=chat, text=self._redact(card.text)[:4000], **extra)
            markup = card_actions.telegram_markup(card)
            if markup is not None:
                body['reply_markup'] = markup.to_dict()
            receipt = await self._channel_outbox.send('send', str(chat), body, None, key, OutboxSource.POST)
            if receipt.result == PostOutcome.REJECTED and markup is not None:
                body.pop('reply_markup')
                receipt = await self._channel_outbox.send('send', str(chat), body, None, key, OutboxSource.POST,
                                                         replace_rejected=True)
            if receipt.success and receipt.external_ids:
                await card_actions.bind(card, receipt.external_ids[0], str(chat))
            return receipt
        try:
            message = await self._app.bot.send_message(chat_id=chat, text=self._redact(card.text)[:4000],
                                                       reply_markup=card_actions.telegram_markup(card), **extra)
        except Exception as exc:
            logger.warning("telegram: a card post to %s failed: %s", target.id, type(exc).__name__)
            return PostReceipt(False, error=redact(str(exc)), outcome=_post_outcome(exc))
        # The chat as the platform answered it, like the message id.
        await card_actions.bind(card, str(message.message_id), str(getattr(message, "chat_id", None) or chat))
        return PostReceipt(True, external_ids=(str(message.message_id),))

    async def edit_card(self, target: NotifyTarget, external_id: str, card: Any) -> PostReceipt:
        """Update a card's text and keyboard, explicitly clearing the keyboard once it is settled."""
        if self._app is None:
            return PostReceipt(False, error="the Telegram bot is not started", outcome=PostOutcome.REJECTED)
        try:
            chat, message_id = int(target.id), int(external_id)
        except ValueError:
            return PostReceipt(False, error="invalid Telegram chat or message id", outcome=PostOutcome.REJECTED)
        try:
            await self._app.bot.edit_message_text(chat_id=chat, message_id=message_id, text=self._redact(card.text)[:4000],
                                                  reply_markup=card_actions.telegram_markup(card))
        except Exception as exc:
            return PostReceipt(False, error=redact(str(exc)), outcome=_post_outcome(exc))
        return PostReceipt(True, external_ids=(external_id,))

    async def edit(self, target: NotifyTarget, external_id: str, text: str) -> PostReceipt:
        if self._app is None:
            return PostReceipt(False, error="the Telegram bot is not started", outcome=PostOutcome.REJECTED)
        try:
            chat, message_id = int(target.id), int(external_id)
        except ValueError:
            return PostReceipt(False, error="invalid Telegram chat or message id", outcome=PostOutcome.REJECTED)
        try:
            await self._app.bot.edit_message_text(chat_id=chat, message_id=message_id, text=self._redact(text))
        except Exception as exc:
            return PostReceipt(False, error=redact(str(exc)), outcome=_post_outcome(exc))
        return PostReceipt(True, external_ids=(external_id,))

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        return await task_notice_delivery.send(envelope, self._deliver)

    async def _deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Show a phase on the message's status line, send task progress or a task's end notice as a message of
        its own, or send the answer to the Telegram user who wrote and close its status line."""
        paths = envelope.metadata.get("attachment_paths", [])
        envelope = replace(envelope, payload={
            key: self._redact(value, paths=paths) if isinstance(value, str) else value
            for key, value in envelope.payload.items()})
        if envelope.message_type is MessageType.CHAT_NOTICE:
            # A task's end, to the chat it recorded, in order behind its progress; sent like an answer.
            try:
                chat = int(envelope.payload.get("chat") or "")
            except ValueError:
                return DeliveryReceipt(channel="telegram", success=False, error="a notice without a Telegram chat id")
            notice = Notice.from_payload(envelope.payload)
            await store_envelope_body(self.channel_id, envelope,
                                      for_group(notice.text) if notice is not None and notice.target.group
                                      else str(envelope.payload.get("text") or ""))
            if notice is None or not notice.target.group:
                return await self._send_answer(chat, str(envelope.payload.get("text") or ""),
                                               message_id=envelope.message_id, source=OutboxSource.NOTICE)
            # A group's notice carries the whole summary: several messages in order. A part that fails ends the
            # notice; the parts before it are not sent again.
            parts = split_message(for_group(notice.text))
            for number, part in enumerate(parts):
                receipt = await self._send_answer(chat, part, message_id=f'{envelope.message_id}:{number}',
                                                  source=OutboxSource.NOTICE)
                if not receipt.success:
                    logger.warning("%s: part %d of %d of a task's end notice to %s failed: %s", self._channel_id,
                                   number + 1, len(parts), chat, receipt.error)
                    return receipt
            return receipt
        corr_id = envelope.correlation_id
        chat_id = self._correlation_chat.get(corr_id)
        if chat_id is None and envelope.target == self._address:
            # Replayed turns and answers outlive the channel instance that received the question.
            route = envelope.metadata.get("reply_route")
            if isinstance(route, dict) and isinstance(route.get("chat_id"), str):
                try:
                    chat_id = int(route["chat_id"])
                except ValueError:
                    pass
                else:
                    if (envelope.message_type is MessageType.STATUS_UPDATE
                            and status_kind(envelope.payload) is StatusKind.PROGRESS):
                        await store_envelope_body(self.channel_id, envelope, str(envelope.payload["text"]),
                                                  chat=str(chat_id), group=chat_id < 0)
                        return await self._send_answer(chat_id, str(envelope.payload["text"]),
                                                       message_id=envelope.message_id, source=OutboxSource.PROGRESS)
                    self._remember(corr_id, chat_id)
        if chat_id is None:
            return DeliveryReceipt(
                channel="telegram",
                success=False,
                error=f"no chat_id for correlation {corr_id}",
                failure=DeliveryFailure.NO_CHAT,
            )

        if envelope.message_type is MessageType.STATUS_UPDATE:
            match status_kind(envelope.payload):
                case StatusKind.PHASE:
                    await self._status.update(corr_id, str(chat_id), envelope.payload)
                case StatusKind.PROGRESS:
                    # Task progress leaves the status line alone.
                    await store_envelope_body(self.channel_id, envelope, str(envelope.payload["text"]),
                                              chat=str(chat_id), group=chat_id < 0)
                    return await self._send_answer(chat_id, str(envelope.payload["text"]),
                                                   message_id=envelope.message_id, source=OutboxSource.PROGRESS)
            return DeliveryReceipt(channel="telegram", success=True)

        text = self._format_reply(envelope)
        if not text:
            return DeliveryReceipt(channel="telegram", success=True)

        text, cards = card_actions.shown(text, envelope.metadata.get("cards"))
        if text:
            await store_envelope_body(self.channel_id, envelope, text, chat=str(chat_id), group=chat_id < 0)
        receipt = (await self._send_answer(chat_id, text, message_id=envelope.message_id) if text
                   else DeliveryReceipt(channel="telegram", success=True))
        for i, card in enumerate(cards):
            gated = chat_settings.outbound_enabled()
            key = derived_key(self.channel_id, f"card:{envelope.message_id}", i) if gated else None
            sent = await self.post_card(NotifyTarget(TargetKind.CHAT, str(chat_id)), card, idempotency_key=key)
            if gated and not sent.success:
                receipt = self._delivery_receipt(sent)
                break
            if not sent.success and not gated:
                await self._send_answer(chat_id, card.text)  # the user can still answer it by typing
            if not text:
                receipt = self._delivery_receipt(sent)
        if not envelope.payload.get("autonomous"):
            await self._status.finish(corr_id, error=envelope.payload.get("error"))
        return receipt

    async def _send_status(self, chat_id: str, text: str) -> str | None:
        if self._app is None:
            return None
        if chat_settings.outbound_enabled():
            receipt = await self._outbox_messages(chat_id, self._redact(text), source=OutboxSource.PROGRESS)
            if not receipt.success:
                raise RuntimeError(receipt.error or 'Telegram status delivery failed')
            return receipt.external_ids[-1] if receipt.external_ids else None
        message = await self._app.bot.send_message(chat_id=int(chat_id), text=self._redact(text))
        return str(message.message_id)

    async def _edit_status(self, chat_id: str, message_id: str, text: str) -> None:
        await self._app.bot.edit_message_text(chat_id=int(chat_id), message_id=int(message_id), text=self._redact(text))

    async def _react(self, chat_id: str, message_id: str) -> None:
        await self._app.bot.set_message_reaction(chat_id=int(chat_id), message_id=int(message_id), reaction="👀")

    async def _turned_off(self, chat_id: int, route: RouteDecision, text: str) -> bool:
        """Refuse an unavailable destination, while allowing fleet stop to reach its older turns."""
        if route.manager_id == "manager" and not route.explicit:
            return False
        try:
            running = self._manager_running(route.manager_id) if self._manager_running is not None else not route.explicit
            if running:
                return False
        except Exception:
            logger.warning("%s: cannot tell whether %s is running", self._channel_id, route.manager_id, exc_info=True)
            if not route.explicit:
                return False
        from ..tiers.manager import TURNED_OFF_REPLY

        reply = TURNED_OFF_REPLY
        if text.strip().lower() == "/stop" and self._stop_chat is not None:
            from ..managers import chat_stop_reply

            reply = chat_stop_reply(await self._stop_chat(self.channel_id, str(chat_id), via="telegram"))
        receipt = await self._send_answer(chat_id, reply)
        if not receipt.success:
            logger.warning("%s: could not tell chat %s that %s is unavailable", self._channel_id, chat_id,
                           route.manager_id)
        log_route(logger, self.channel_id, str(chat_id), route, reason="disabled_target",
                  outcome="answered" if receipt.success else "answer_failed")
        return True

    @staticmethod
    def _delivery_receipt(receipt: PostReceipt) -> DeliveryReceipt:
        return DeliveryReceipt(channel='telegram', success=receipt.success,
                               external_id=receipt.external_ids[-1] if receipt.external_ids else None,
                               error=redact(receipt.error) if receipt.error else None)

    async def _outbox_messages(self, chat: str, text: str, *, key: str | None = None,
                               message_id: str | None = None, reply_to: int | None = None,
                               source: OutboxSource, formatted: bool = False) -> PostReceipt:
        from telegram.constants import ParseMode

        assert self._channel_outbox is not None
        try:
            chat_id = int(chat)
        except ValueError:
            return PostReceipt(False, error=f'not a Telegram chat id: {chat!r}', outcome=PostOutcome.REJECTED)
        seed = message_id or key or str(uuid4())
        sent: list[str] = []
        for i, chunk in enumerate(self._split_message(text, max_len=4000)):
            part_key = key if key is not None and i == 0 else derived_key(self.channel_id, seed, i)
            body: dict[str, Any] = {'chat_id': chat_id, 'text': chunk}
            if formatted:
                body['parse_mode'] = ParseMode.MARKDOWN_V2
            if reply_to is not None and i == 0:
                body.update(reply_to_message_id=reply_to, allow_sending_without_reply=True)
            receipt = await self._channel_outbox.send('send', chat, body, None, part_key, source)
            if formatted and receipt.result == PostOutcome.REJECTED:
                body.pop('parse_mode')
                receipt = await self._channel_outbox.send('send', chat, body, None, part_key, source,
                                                         replace_rejected=True)
            sent.extend(receipt.external_ids)
            if not receipt.success:
                return replace(receipt, external_ids=tuple(sent), error=redact(receipt.error or 'delivery failed'))
        return PostReceipt(True, external_ids=tuple(sent))

    async def _send_answer(self, chat_id: int, text: str, *, message_id: str | None = None,
                           source: OutboxSource = OutboxSource.ANSWER) -> DeliveryReceipt:
        """Send ``text`` as Markdown, or as plain text when Telegram cannot parse it."""
        import telegram

        text = self._redact(text)
        if chat_settings.outbound_enabled():
            return self._delivery_receipt(await self._outbox_messages(
                str(chat_id), text, message_id=message_id, source=source, formatted=True))
        try:
            bot = self._app.bot
            # Split long messages (Telegram limit: 4096 chars)
            for chunk in self._split_message(text, max_len=4000):
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=telegram.constants.ParseMode.MARKDOWN_V2,
                )
            return DeliveryReceipt(
                channel="telegram",
                success=True,
                external_id=str(msg.message_id),
            )
        except Exception:
            # Fallback: try without markdown if parse fails
            try:
                for chunk in self._split_message(text, max_len=4000):
                    msg = await bot.send_message(chat_id=chat_id, text=chunk)
                return DeliveryReceipt(
                    channel="telegram",
                    success=True,
                    external_id=str(msg.message_id),
                )
            except Exception as inner:
                logger.warning("telegram delivery failed: %s", inner)
                return DeliveryReceipt(
                    channel="telegram", success=False, error=redact(str(inner))
                )

    def _redact(self, text: str, *, paths: Sequence[str] = ()) -> str:
        return chat_attachments.redact(text, self._bus.runtime_root, paths=paths) if self._bus else text

    def healthcheck(self) -> dict[str, Any]:
        return {
            "channel": "telegram",
            "healthy": self._running and self._app is not None,
            "active_conversations": len(self._correlation_chat),
        }

    def _remember(self, correlation_id: str, chat_id: int) -> None:
        """Map a correlation to its chat for reply routing, bounded to the newest ``_CORRELATION_MAX``."""
        self._correlation_chat[correlation_id] = chat_id
        while len(self._correlation_chat) > _CORRELATION_MAX:
            self._correlation_chat.popitem(last=False)

    def _admits(self, update: Any, chat_senders: frozenset[str] = frozenset()) -> bool:
        """Whether the update's sender is on the allowlist; anyone else is logged and gets no reply."""
        user = getattr(update, "effective_user", None)
        return self._allowlist.admits(getattr(user, "id", None), chat_senders=chat_senders)

    def _acknowledge(self, update: Any) -> None:
        """React to a direct message the Manager gets, in the background: the bot saw it."""
        chat = update.effective_chat
        if getattr(chat, "type", None) == "private" and update.message is not None:
            self._reactions.add(str(chat.id), str(update.message.message_id))

    # --- Telegram handlers ---

    async def _ignore_update(self, update: Any, context: Any) -> None:
        pass

    def _durable(self, callback: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        async def handle(update: Any, context: Any) -> None:
            assert self._inbox is not None
            try:
                if await self._inbox.processed(update.update_id):
                    logger.info("%s: duplicate Telegram update_id=%s ignored", self.channel_id, update.update_id)
                    return
                message = update.effective_message
                # A button's press is as old as the card it is on; the card's own expiry bounds it.
                date = getattr(message, "date", None) if getattr(update, "callback_query", None) is None else None
                age = (datetime.now(timezone.utc) - date).total_seconds() if date is not None else 0
                age_at_stop = await self._inbox.age_at_stop(update.update_id)
                if age_at_stop is not None:
                    age = age_at_stop
                if age > self._backlog_max_age_seconds:
                    logger.warning("%s: stale Telegram update_id=%s age_seconds=%.0f backlog_max_age_seconds=%s ignored",
                                   self.channel_id, update.update_id, age, self._backlog_max_age_seconds)
                else:
                    for attempt in range(3):
                        try:
                            await callback(update, context)
                        except Exception:
                            if (attempt < 2 and update.update_id not in self._reached_publish and self._running):
                                await asyncio.sleep(attempt + 1)
                                if self._running:
                                    continue
                            logger.exception("%s: Telegram update_id=%s handler failed", self.channel_id, update.update_id)
                            raise
                        break
                await self._inbox.finish(update.update_id)
            finally:
                self._reached_publish.discard(update.update_id)
        return handle

    async def _admitted(self, update: Any, *, text_required: bool = False) -> RouteDecision | None:
        chat_id = update.effective_chat.id
        route = (self._resolve_chat(self.channel_id, str(chat_id)) if self._resolve_chat is not None
                 else RouteDecision(RouteAction.ADMIT, self._manager_id))
        if not self._admits(update, route.senders):
            return None
        if text_required and (not update.message or not update.message.text):
            return None
        if self._resolve_chat is not None:
            await observe_chat(self.channel_id, str(chat_id), suppressed=route.action is RouteAction.SILENT)
        if route.action is RouteAction.SILENT:
            log_route(logger, self.channel_id, str(chat_id), route)
        return route if route.action is RouteAction.ADMIT else None

    async def _cmd_start(self, update: Any, context: Any) -> None:
        route = await self._admitted(update)
        if route is None or (route.explicit and await self._turned_off(update.effective_chat.id, route, "/start")):
            return
        log_route(logger, self.channel_id, str(update.effective_chat.id), route)
        text = ("Looper Agent 已就绪。直接发消息给我，或使用:\n"
            "/status — 查看当前任务进度\n"
            "/new — 开始新的 Manager 会话\n"
            "/stop — 停止管家当前的回复\n"
            "/profile — 查看或切换本会话的 Agent Profile\n"
            "/help — 帮助信息"
        )
        await self._command_reply(update, text)

    async def _cmd_status(self, update: Any, context: Any) -> None:
        """``/status`` is the typed command while the status card is on; off, it is still the progress question."""
        from .. import status_card

        try:
            on = status_card.enabled()
        except Exception:
            logger.warning("%s: cannot read whether /status is on; forwarded as before", self._channel_id,
                           exc_info=True)
            on = False
        if on:
            await self._forward_command(update)
            return
        await self._forward(update, "当前任务进度怎么样？", status=True)

    async def _cmd_help(self, update: Any, context: Any) -> None:
        route = await self._admitted(update)
        if route is None or (route.explicit and await self._turned_off(update.effective_chat.id, route, "/help")):
            return
        log_route(logger, self.channel_id, str(update.effective_chat.id), route)
        await self._command_reply(update, chat_commands.help_text())

    async def _command_reply(self, update: Any, text: str) -> None:
        if chat_settings.outbound_enabled():
            receipt = await self._outbox_messages(
                str(update.effective_chat.id), text, reply_to=update.message.message_id,
                message_id=f'command:{update.update_id}', source=OutboxSource.ANSWER)
            if not receipt.success:
                raise RuntimeError(receipt.error or 'Telegram command reply failed')
            return
        await update.message.reply_text(text)

    async def _cmd_new(self, update: Any, context: Any) -> None:
        """/new goes to the Manager as the text ``/new``, which it answers itself by starting a new session."""
        await self._forward(update, "/new")

    async def _cmd_stop(self, update: Any, context: Any) -> None:
        """/stop goes to the selected Manager as text, which it answers itself, outside its queue."""
        await self._forward(update, "/stop")

    async def _cmd_gates(self, update: Any, context: Any) -> None:
        """/gates goes to the Manager as typed; it lists the gates and answers itself."""
        await self._forward_command(update)

    async def _cmd_gate(self, update: Any, context: Any) -> None:
        """/gate N <answer> and /gate N end go to the Manager as typed, which answers them itself."""
        await self._forward_command(update)

    async def _forward_command(self, update: Any) -> None:
        await self._forward(update, None)

    async def _cmd_profile(self, update: Any, context: Any) -> None:
        """/profile [choice] goes to the Manager as text, which it answers itself; ``CommandHandler`` has already
        dropped any ``@<bot>`` suffix, so the text is rebuilt from the parsed arguments."""
        args = getattr(context, "args", None) or []
        await self._forward(update, "/profile" + (" " + " ".join(args) if args else ""))

    async def _on_button(self, update: Any, context: Any) -> None:
        """A press of a card's button (``card_actions.respond``): a toast, and the card as it is now; a card whose
        action ends later is edited then. Only a sender this bot admits in that chat may press. While card buttons
        are off the press is left unanswered, as before cards."""
        started = asyncio.get_running_loop().time()
        try:
            if not await asyncio.wait_for(card_actions.enabled(), card_actions.AUTHORIZE_SECONDS):
                return
        except Exception:
            logger.warning("%s: cannot tell whether card buttons are on; a press is not answered", self.channel_id,
                           exc_info=True)
            return
        query = update.callback_query
        chat = getattr(update.effective_chat, "id", None)
        route = (self._resolve_chat(self.channel_id, str(chat)) if self._resolve_chat is not None and chat is not None
                 else None)
        if not self._admits(update, route.senders if route is not None else frozenset()):
            logger.info("%s: a card press by an unlisted sender was refused", self.channel_id)
            await self._answer_press(query, card_actions.Answer(card_actions.NOT_ALLOWED["confirmation"], "error"))
            return
        message = getattr(query, "message", None)

        async def update_card(card: card_actions.Card) -> None:
            if message is not None and chat is not None:
                await self._app.bot.edit_message_text(chat_id=chat, message_id=message.message_id,
                                                      text=self._redact(card.text)[:4000],
                                                      reply_markup=card_actions.telegram_markup(card))

        answer = await card_actions.respond(
            card_actions.Press(self.channel_id, str(update.effective_user.id), str(query.data or ""),
                               chat="" if chat is None else str(chat),
                               message_id=str(message.message_id) if message is not None else ""), update_card,
            started=started)
        await self._answer_press(query, answer)
        if answer.card is not None:
            try:
                await update_card(answer.card)
            except Exception as exc:
                logger.warning("%s: could not edit a card after its press: %s", self.channel_id, type(exc).__name__)
        card_actions.follow(answer, update_card)

    @staticmethod
    async def _answer_press(query: Any, answer: card_actions.Answer) -> None:
        try:
            await query.answer(text=answer.toast[:200])
        except Exception as exc:
            logger.info("telegram: a card press's answer failed: %s", type(exc).__name__)

    async def _cmd_todo(self, update: Any, context: Any) -> None:
        """/todo goes to the Manager as typed while the "needs you" summary is on (``attention``); off, it is dropped
        like any other unknown command."""
        from .. import attention

        try:
            on = await attention.enabled()
        except Exception:
            logger.warning("%s: cannot read whether /todo is on; dropped", self._channel_id, exc_info=True)
            return
        if on:
            await self._forward_command(update)

    async def _on_message(self, update: Any, context: Any) -> None:
        await self._forward_command(update)

    async def _forward(self, update: Any, text: str | None, *, status: bool = False) -> None:
        """Pin admission once before observation or any visible side effect."""
        route = await self._admitted(update, text_required=text is None)
        if route is None:
            return
        text = update.message.text if text is None else text
        assert self._bus is not None
        import uuid as _uuid

        if await self._turned_off(update.effective_chat.id, route, text):
            return
        log_route(logger, self.channel_id, str(update.effective_chat.id), route,
                  via_chat_senders=str(update.effective_user.id) not in self._allowlist.ids)
        self._acknowledge(update)
        chat_id = update.effective_chat.id
        correlation_id = str(_uuid.uuid4())
        self._remember(correlation_id, chat_id)

        context: dict[str, Any] = {"chat_id": chat_id}
        if not status:
            context.update(user_id=update.effective_user.id, username=update.effective_user.username or "",
                           sender_kind="bot" if getattr(update.effective_user, "is_bot", False) is True else "person")
            if update.message is not None:  # None for an edited /new
                context["message_id"] = str(update.message.message_id)
                # Keep provenance without copying the original sender's identity or quoted content.
                for key in ("forward_origin", "quote", "external_reply"):
                    if getattr(update.message, key, None) is not None:
                        context[key] = True
                replied = getattr(update.message, "reply_to_message", None)
                if replied is not None:
                    context["reply_to_message_id"] = str(replied.message_id)
        metadata = {"origin_channel_id": self.channel_id}
        if self._resolve_chat is not None:
            metadata["channel_manager_id"] = self._manager_id
            metadata["channel_thread_id"] = channel_thread_id(self.channel_id, str(chat_id), route.manager_id,
                                                              self._manager_id)
        envelope = MessageEnvelope.create(
            source=self._address,
            target=manager_address(route.manager_id),
            message_type=MessageType.USER_MESSAGE,
            payload={"text": text, "language": "zh", "channel_context": context},
            correlation_id=correlation_id,
            metadata=metadata,
        )
        self._reached_publish.add(update.update_id)
        await self._bus.publish(envelope)

    # --- Outbound: Bus → Telegram ---

    async def _outbound_loop(self) -> None:
        assert self._bus is not None
        try:
            async for envelope in self._bus.subscribe(self.channel_id):
                if not self._running:
                    break
                # An answer is stored once it was sent, or as not delivered (the Manager names its record).
                receipt = await self.deliver(envelope)
                self.record_envelope_delivery(envelope, receipt)
                if envelope.metadata.get("proactive"):
                    route = envelope.metadata.get("reply_route")
                    route = route if isinstance(route, dict) else {}
                    chat = str(route.get("chat_id") or self._correlation_chat.get(envelope.correlation_id) or "")
                    record_delivery(self.channel_id, envelope, receipt, chat=chat, group=chat.startswith("-"))
                await record_reply(self.channel_type, envelope, receipt,
                                   runtime_root=self._bus.runtime_root)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _format_reply(envelope: MessageEnvelope) -> str:
        """The text of an answer; a status is handled by ``deliver``."""
        match envelope.message_type:
            case MessageType.AGENT_REPLY | MessageType.CLARIFICATION_REQUEST:
                return envelope.payload.get("text", "")
            case _:
                return ""

    @staticmethod
    def _split_message(text: str, *, max_len: int = 4000) -> list[str]:
        if len(text) <= max_len:
            return [text]
        chunks: list[str] = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Try to split at newline
            cut = text.rfind("\n", 0, max_len)
            if cut < max_len // 2:
                cut = max_len
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks
