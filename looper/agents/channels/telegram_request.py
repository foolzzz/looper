"""Telegram's non-polling request seam; reads and disabled writes stay untouched."""
from __future__ import annotations

import json
from contextvars import ContextVar
from enum import StrEnum
from typing import Any

import httpx
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

from .outbound import ErrorClass, WriteKind


class TelegramMethod(StrEnum):
    SEND_MESSAGE = 'sendMessage'
    EDIT_MESSAGE = 'editMessageText'
    REACTION = 'setMessageReaction'
    DOCUMENT = 'sendDocument'
    PHOTO = 'sendPhoto'
    ACTION = 'sendChatAction'
    COMMANDS = 'setMyCommands'
    CALLBACK = 'answerCallbackQuery'


WRITE_METHODS = {
    TelegramMethod.SEND_MESSAGE: WriteKind.SEND,
    TelegramMethod.EDIT_MESSAGE: WriteKind.EDIT,
    TelegramMethod.REACTION: WriteKind.REACT,
    TelegramMethod.DOCUMENT: WriteKind.SEND,
    TelegramMethod.PHOTO: WriteKind.SEND,
    TelegramMethod.ACTION: WriteKind.REACT,
    TelegramMethod.COMMANDS: WriteKind.REACT,
    TelegramMethod.CALLBACK: WriteKind.REACT,
}

# Outbox already owns the gateway slot and persisted retry schedule. Classify
# its HTTP attempt here, without acquiring the same slot a second time.
outbox_attempt: ContextVar[bool] = ContextVar('telegram_outbox_attempt', default=False)


class TelegramWriteError(TelegramError):
    def __init__(self, message: str, error_class: ErrorClass, retry_after: float | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.retry_after = retry_after


class GatedRequest(HTTPXRequest):
    def __init__(self, gateway, *, enabled, inner=None, **kwargs):
        super().__init__(**kwargs)
        self.gateway, self.enabled, self.inner = gateway, enabled, inner

    async def do_request(self, url: str, **kwargs: Any) -> tuple[int, bytes]:
        raw = self.inner or super().do_request
        method = url.rsplit('/', 1)[-1]
        if method not in WRITE_METHODS or not (self.enabled() or outbox_attempt.get()):
            return await raw(url=url, **kwargs)

        async def call():
            try:
                code, payload = await raw(url=url, **kwargs)
            except Exception as exc:
                cause = exc.__cause__ or exc
                safe = isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                raise TelegramWriteError(str(exc), ErrorClass.TRANSIENT if safe else ErrorClass.UNKNOWN) from exc
            if code >= 400:
                try:
                    response = json.loads(payload)
                except (ValueError, TypeError):
                    response = {}
                if not isinstance(response, dict):
                    response = {}
                parameters = response.get('parameters')
                retry_after = parameters.get('retry_after') if isinstance(parameters, dict) else None
                classification = (ErrorClass.RATE_LIMITED if code == 429 else
                                  ErrorClass.REJECTED if code < 500 else ErrorClass.UNKNOWN)
                raise TelegramWriteError(response.get('description') or f'Telegram HTTP {code}',
                                         classification, retry_after)
            return code, payload

        if outbox_attempt.get():
            return await call()
        if method == TelegramMethod.EDIT_MESSAGE:
            request_data = kwargs.get('request_data')
            data = request_data.parameters if request_data else {}
            return await self.gateway.edit(f"{data.get('chat_id')}:{data.get('message_id')}", call)
        return await self.gateway.write(WRITE_METHODS[method], call)


class TelegramOutboxTransport:
    def __init__(self, bot):
        self.bot = bot

    async def send(self, *, idempotency_key: str, return_id: bool, **body) -> str:
        token = outbox_attempt.set(True)
        try:
            message = await self.bot().send_message(**body)
            return str(message.message_id)
        finally:
            outbox_attempt.reset(token)
