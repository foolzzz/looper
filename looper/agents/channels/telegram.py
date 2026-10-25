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
import logging
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from ... import env
from ..bus import MessageBus
from ..log_redaction import add_secret, redact
from ..message import (
    AgentAddress,
    MessageEnvelope,
    MessageType,
    channel_address,
    channel_thread_id,
    manager_address,
    parse_channel_id,
)
from ..task_notice import NotifyTarget
from .allowlist import SenderAllowlist
from .. import chat_commands, chat_settings
from .base import ChannelAdapter, DeliveryFailure, DeliveryReceipt, PostReceipt
from .chat_history import record_reply
from .chat_routing import ChatResolver, RouteAction, RouteDecision, observe_chat
from .status_line import Reactions, StatusKind, StatusLines, status_kind

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
        # Only the built-in row falls back to the environment; an added bot has its own token.
        self._bot_token = bot_token or (env.get("LOOPER_TELEGRAM_BOT_TOKEN", "") if channel_id is None else "")
        self._allowlist = allowlist  # Telegram user ids, as strings
        self._bus: MessageBus | None = None
        self._address = channel_address("telegram", self._channel_id)
        self._app: Any = None
        self._requests: tuple[Any, ...] = ()
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
            CommandHandler,
            MessageHandler,
            filters,
        )
        from telegram.request import HTTPXRequest

        self._bus = bus
        self._running = True

        add_secret(self._bot_token)  # python-telegram-bot puts it in every request URL and in the Bot's repr
        # Own both transports: Application.shutdown is a no-op if initialize failed.
        # Match ApplicationBuilder's default pool sizes for polling and other calls.
        self._requests = (HTTPXRequest(connection_pool_size=1), HTTPXRequest(connection_pool_size=256))
        builder = Application.builder().token(self._bot_token)
        builder.get_updates_request(self._requests[0])
        builder.request(self._requests[1])
        self._app = builder.build()

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        # Only /new itself (also /new@<this bot>): "/new foo" stays an unknown command, as before.
        self._app.add_handler(CommandHandler("new", self._cmd_new, has_args=False))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop, has_args=False))
        # The gate commands take arguments; the Manager parses and answers them in code, so the text goes as typed.
        self._app.add_handler(CommandHandler("gates", self._cmd_gates))
        self._app.add_handler(CommandHandler("gate", self._cmd_gate))
        self._app.add_handler(CommandHandler("profile", self._cmd_profile))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        self._outbound_task = asyncio.create_task(self._outbound_loop())

        try:
            await self._app.initialize()
            try:
                from telegram import BotCommand

                await self._app.bot.set_my_commands(
                    [BotCommand(name, desc) for name, desc in bot_commands()]
                )
            except Exception:
                logger.warning("telegram set_my_commands failed", exc_info=True)
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]
        except BaseException:
            await self.stop()
            raise
        logger.info("telegram channel adapter started")

    async def stop(self) -> None:
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
        if self._app:
            await cleanup("shutdown", self._app.shutdown)
        for request in self._requests:
            await cleanup("request.shutdown", request.shutdown)
        self._app = None
        self._requests = ()
        self._outbound_task = None
        self._bus = None
        logger.info("telegram channel adapter stopped")

    async def notify(self, chat: str, text: str) -> DeliveryReceipt:
        """Send ``text`` as plain text to Telegram chat ``chat`` (a user's private chat has the user's id)."""
        if self._app is None:
            return DeliveryReceipt(channel="telegram", success=False, error="the Telegram bot is not started")
        try:
            for chunk in self._split_message(text, max_len=4000):
                msg = await self._app.bot.send_message(chat_id=int(chat), text=chunk)
            return DeliveryReceipt(channel="telegram", success=True, external_id=str(msg.message_id))
        except Exception as exc:
            logger.warning("telegram notification to %s failed: %s", chat, exc)
            return DeliveryReceipt(channel="telegram", success=False, error=redact(str(exc)))

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        """Send ``text`` as plain text to the Telegram chat ``target.id``, in messages of at most 4,000 characters,
        the first a reply to the message ``reply_to`` when given (sent on its own when that message is gone). The
        ids of the messages sent are kept when a later one fails, so replies to them still route. Telegram has no
        idempotency key: a post retried after a lost answer may be sent twice."""
        if self._app is None:
            return PostReceipt(False, error="the Telegram bot is not started")
        try:
            chat = int(target.id)
        except ValueError:
            return PostReceipt(False, error=f"not a Telegram chat id: {target.id!r}")
        try:
            reply = int(reply_to) if reply_to else None
        except ValueError:
            return PostReceipt(False, error=f"not a Telegram message id: {reply_to!r}")
        sent: list[str] = []
        try:
            for chunk in self._split_message(text, max_len=4000):
                extra = ({"reply_to_message_id": reply, "allow_sending_without_reply": True}
                         if reply is not None and not sent else {})
                message = await self._app.bot.send_message(chat_id=chat, text=chunk, **extra)
                sent.append(str(message.message_id))
        except Exception as exc:
            error = redact(str(exc))
            logger.warning("telegram: a post to %s failed (%d of its messages sent): %s", target.id, len(sent), error)
            return PostReceipt(False, external_ids=tuple(sent), error=error)
        return PostReceipt(True, external_ids=tuple(sent))

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Show a phase on the message's status line, send task progress or a task's end notice as a message of
        its own, or send the answer to the Telegram user who wrote and close its status line."""
        if envelope.message_type is MessageType.CHAT_NOTICE:
            # A task's end, to the chat it recorded, in order behind its progress; sent like an answer.
            try:
                chat = int(envelope.payload.get("chat") or "")
            except ValueError:
                return DeliveryReceipt(channel="telegram", success=False, error="a notice without a Telegram chat id")
            return await self._send_answer(chat, str(envelope.payload.get("text") or ""))
        corr_id = envelope.correlation_id
        chat_id = self._correlation_chat.get(corr_id)
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
                    return await self._send_answer(chat_id, str(envelope.payload["text"]))
            return DeliveryReceipt(channel="telegram", success=True)

        text = self._format_reply(envelope)
        if not text:
            return DeliveryReceipt(channel="telegram", success=True)

        receipt = await self._send_answer(chat_id, text)
        if not envelope.payload.get("autonomous"):
            await self._status.finish(corr_id, error=envelope.payload.get("error"))
        return receipt

    async def _send_status(self, chat_id: str, text: str) -> str | None:
        if self._app is None:
            return None
        message = await self._app.bot.send_message(chat_id=int(chat_id), text=text)
        return str(message.message_id)

    async def _edit_status(self, chat_id: str, message_id: str, text: str) -> None:
        await self._app.bot.edit_message_text(chat_id=int(chat_id), message_id=int(message_id), text=text)

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
        return True

    async def _send_answer(self, chat_id: int, text: str) -> DeliveryReceipt:
        """Send ``text`` as Markdown, or as plain text when Telegram cannot parse it."""
        import telegram

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
        except Exception as exc:
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

    def _admits(self, update: Any) -> bool:
        """Whether the update's sender is on the allowlist; anyone else is logged and gets no reply."""
        user = getattr(update, "effective_user", None)
        return self._allowlist.admits(getattr(user, "id", None))

    def _acknowledge(self, update: Any) -> None:
        """React to a direct message the Manager gets, in the background: the bot saw it."""
        chat = update.effective_chat
        if getattr(chat, "type", None) == "private" and update.message is not None:
            self._reactions.add(str(chat.id), str(update.message.message_id))

    # --- Telegram handlers ---

    async def _admitted(self, update: Any, *, text_required: bool = False) -> RouteDecision | None:
        if not self._admits(update):
            return None
        if text_required and (not update.message or not update.message.text):
            return None
        chat_id = update.effective_chat.id
        route = (self._resolve_chat(self.channel_id, str(chat_id)) if self._resolve_chat is not None
                 else RouteDecision(RouteAction.ADMIT, self._manager_id))
        if self._resolve_chat is not None:
            await observe_chat(self.channel_id, str(chat_id), suppressed=route.action is RouteAction.SILENT)
        return route if route.action is RouteAction.ADMIT else None

    async def _cmd_start(self, update: Any, context: Any) -> None:
        route = await self._admitted(update)
        if route is None or (route.explicit and await self._turned_off(update.effective_chat.id, route, "/start")):
            return
        await update.message.reply_text(
            "Looper Agent 已就绪。直接发消息给我，或使用:\n"
            "/status — 查看当前任务进度\n"
            "/new — 开始新的 Manager 会话\n"
            "/stop — 停止管家当前的回复\n"
            "/profile — 查看或切换本会话的 Agent Profile\n"
            "/help — 帮助信息"
        )

    async def _cmd_status(self, update: Any, context: Any) -> None:
        await self._forward(update, "当前任务进度怎么样？", status=True)

    async def _cmd_help(self, update: Any, context: Any) -> None:
        route = await self._admitted(update)
        if route is None or (route.explicit and await self._turned_off(update.effective_chat.id, route, "/help")):
            return
        await update.message.reply_text(chat_commands.help_text())

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
        self._acknowledge(update)
        chat_id = update.effective_chat.id
        correlation_id = str(_uuid.uuid4())
        self._remember(correlation_id, chat_id)

        context: dict[str, Any] = {"chat_id": chat_id}
        if not status:
            context.update(user_id=update.effective_user.id, username=update.effective_user.username or "")
            if update.message is not None:  # None for an edited /new
                context["message_id"] = str(update.message.message_id)
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
        await self._bus.publish(envelope)

    # --- Outbound: Bus → Telegram ---

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
