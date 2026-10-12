"""Telegram Bot Channel Adapter.

Bridges Telegram Bot API with the agent Bus. Uses long-polling by default
(webhook mode planned). ``loopx serve`` builds this adapter from the
``channel-telegram`` row that the dashboard Settings page edits.

Fallbacks when the constructor arguments are empty:
  LOOPX_TELEGRAM_BOT_TOKEN — Bot token from @BotFather
  LOOPX_TELEGRAM_ALLOWED_USERS — Comma-separated Telegram user IDs (empty = allow all)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

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


def parse_allowed_user_ids(raw: str) -> set[int]:
    """Parse comma-separated Telegram user IDs; raise ValueError on a bad entry."""
    return {int(part) for part in (p.strip() for p in raw.split(",")) if part}


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
        bot_token: str | None = None,
        allowed_user_ids: set[int] | None = None,
    ) -> None:
        self._bot_token = bot_token or os.environ.get("LOOPX_TELEGRAM_BOT_TOKEN", "")
        self._allowed_user_ids = allowed_user_ids or parse_allowed_user_ids(
            os.environ.get("LOOPX_TELEGRAM_ALLOWED_USERS", "")
        )
        self._bus: MessageBus | None = None
        self._address = channel_address("telegram", "channel-telegram")
        self._app: Any = None
        self._outbound_task: asyncio.Task[Any] | None = None
        self._running = False
        # correlation_id → telegram chat_id for reply routing
        self._correlation_chat: dict[str, int] = {}

    @property
    def channel_type(self) -> str:
        return "telegram"

    @property
    def channel_id(self) -> str:
        return "channel-telegram"

    async def start(self, bus: MessageBus) -> None:
        if not _check_telegram_available():
            logger.error(
                "python-telegram-bot not installed; it is a default LoopX "
                "dependency, so reinstall LoopX (for a checkout: uv sync)"
            )
            return

        if not self._bot_token:
            logger.error("LOOPX_TELEGRAM_BOT_TOKEN not set, telegram adapter disabled")
            return

        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )

        self._bus = bus
        self._running = True

        self._app = (
            Application.builder()
            .token(self._bot_token)
            .build()
        )

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        self._outbound_task = asyncio.create_task(self._outbound_loop())

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]
        logger.info("telegram channel adapter started")

    async def stop(self) -> None:
        self._running = False
        if self._outbound_task:
            self._outbound_task.cancel()
            try:
                await self._outbound_task
            except asyncio.CancelledError:
                pass
        if self._app:
            await self._app.updater.stop()  # type: ignore[union-attr]
            await self._app.stop()
            await self._app.shutdown()
        logger.info("telegram channel adapter stopped")

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        """Send a message to the Telegram user who started the conversation."""
        import telegram

        corr_id = envelope.correlation_id
        chat_id = self._correlation_chat.get(corr_id)
        if chat_id is None:
            return DeliveryReceipt(
                channel="telegram",
                success=False,
                error=f"no chat_id for correlation {corr_id}",
            )

        text = self._format_reply(envelope)
        if not text:
            return DeliveryReceipt(channel="telegram", success=True)

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
                    channel="telegram", success=False, error=str(inner)
                )

    def healthcheck(self) -> dict[str, Any]:
        return {
            "channel": "telegram",
            "healthy": self._running and self._app is not None,
            "active_conversations": len(self._correlation_chat),
        }

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed_user_ids:
            return True
        return user_id in self._allowed_user_ids

    # --- Telegram handlers ---

    async def _cmd_start(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update.effective_user.id):
            await update.message.reply_text("未授权的用户。")
            return
        await update.message.reply_text(
            "LoopX Agent 已就绪。直接发消息给我，或使用:\n"
            "/status — 查看当前任务进度\n"
            "/help — 帮助信息"
        )

    async def _cmd_status(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update.effective_user.id):
            return
        assert self._bus is not None
        import uuid as _uuid

        correlation_id = str(_uuid.uuid4())
        chat_id = update.effective_chat.id
        self._correlation_chat[correlation_id] = chat_id

        envelope = MessageEnvelope.create(
            source=self._address,
            target=MANAGER_ADDRESS,
            message_type=MessageType.USER_MESSAGE,
            payload={
                "text": "当前任务进度怎么样？",
                "language": "zh",
                "channel_context": {"chat_id": chat_id},
            },
            correlation_id=correlation_id,
            metadata={"origin_channel_id": self.channel_id},
        )
        await self._bus.publish(envelope)

    async def _cmd_help(self, update: Any, context: Any) -> None:
        await update.message.reply_text(
            "LoopX Agent 命令:\n"
            "直接发消息 — 提需求或问问题\n"
            "/status — 查看当前任务进度\n"
            "/help — 显示此帮助"
        )

    async def _on_message(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update.effective_user.id):
            return
        if not update.message or not update.message.text:
            return
        assert self._bus is not None
        import uuid as _uuid

        text = update.message.text
        chat_id = update.effective_chat.id
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
                    "user_id": update.effective_user.id,
                    "username": update.effective_user.username or "",
                },
            },
            correlation_id=correlation_id,
            metadata={"origin_channel_id": self.channel_id},
        )
        await self._bus.publish(envelope)

    # --- Outbound: Bus → Telegram ---

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
    def _format_reply(envelope: MessageEnvelope) -> str:
        match envelope.message_type:
            case MessageType.AGENT_REPLY | MessageType.CLARIFICATION_REQUEST:
                return envelope.payload.get("text", "")
            case MessageType.STATUS_UPDATE:
                text = envelope.payload.get("text", "")
                if envelope.payload.get("delta"):
                    return ""  # Don't send streaming deltas to Telegram
                return text
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
