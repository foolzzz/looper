"""The Lark bot-token transport and its channel adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .allowlist import SenderAllowlist
from .lark import LarkChannelAdapter, RejectedListener
from .lark_bot_settings import stored_config


class BotTransport:
    """Use the open platform for messages and one deduplication set across connections."""

    def __init__(self, app_id: str, app_secret: str, *, base_url: str | None = None,
                 admit: Callable[[dict[str, Any]], bool] | None = None) -> None:
        from .lark_api import LarkApi
        from .lark_stream import SeenEvents, StreamStats

        self._api = LarkApi(app_id, app_secret, **({"base_url": base_url} if base_url is not None else {}))
        self._seen = SeenEvents()
        self._admit = admit
        self._stats = StreamStats()

    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        return await self._api.send(chat_id=chat_id, user_id=user_id, text=text, markdown=markdown,
                                    idempotency_key=idempotency_key)

    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        return await self._api.reply(message_id, text=text, markdown=markdown, idempotency_key=idempotency_key)

    async def edit(self, message_id: str, *, text: str) -> None:
        await self._api.edit(message_id, text=text)

    async def react(self, message_id: str) -> None:
        await self._api.react(message_id)

    def events(self) -> AsyncIterator[dict[str, Any]]:
        from .lark_stream import stream_events

        return stream_events(self._api, seen=self._seen, admit=self._admit, stats=self._stats)

    async def names(self, open_ids: Iterable[str]) -> dict[str, str]:
        return await self._api.names(open_ids)

    def available(self) -> bool:
        return True

    async def aclose(self) -> None:
        await self._api.aclose()


def make_adapter(config: Mapping[str, Any], app_secret: str, *, runtime_root: Path,
                 allowlist: SenderAllowlist, on_rejected: RejectedListener | None,
                 bot_open_id: str, base_url: str | None = None) -> LarkChannelAdapter:
    """Build a bot-token channel with its app, sender policy and event identity."""
    from .lark_api import BASE_URLS

    config = stored_config(config)
    transport = BotTransport(config["app_id"], app_secret, base_url=base_url or BASE_URLS[config["domain"]],
                             admit=lambda event: event["sender_id"] in allowlist.ids)
    return LarkChannelAdapter(channel_type="lark_bot", transport=transport, runtime_root=runtime_root,
                              allowlist=allowlist, on_rejected=on_rejected, bot_open_id=bot_open_id, backoff=True)
