"""The Lark bot-token transport and its channel adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .allowlist import SenderAllowlist
from .chat_routing import ChatResolver
from .lark import LarkChannelAdapter, RejectedListener
from .lark_bot_settings import stored_config
from .lark_content import ResourceRef


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

    async def download(self, message_id: str, ref: ResourceRef, part: Path, *, max_bytes: int) -> None:
        await self._api.download(message_id, ref.key, ref.kind.value, part, max_bytes=max_bytes)

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
                 bot_open_id: str, base_url: str | None = None,
                 channel_id: str | None = None, manager_id: str = "manager",
                 manager_running: Callable[[str], bool] | None = None,
                 resolve_chat: ChatResolver | None = None,
                 stop_chat: Callable[..., Awaitable[Any]] | None = None) -> LarkChannelAdapter:
    """Build a bot-token channel with its app, sender policy and event identity."""
    from .lark_api import BASE_URLS

    def admit(event: dict[str, Any]) -> bool:
        # The registry installs the live resolver on the adapter after construction.
        decision = (adapter._resolve_chat(adapter.channel_id, event["chat_id"])
                    if adapter._resolve_chat is not None else None)
        sender_id = event["sender_id"]
        return sender_id in allowlist.ids or (decision is not None and sender_id in decision.senders)

    config = stored_config(config)
    transport = BotTransport(config["app_id"], app_secret, base_url=base_url or BASE_URLS[config["domain"]],
                             admit=admit)
    adapter = LarkChannelAdapter(channel_type="lark_bot", transport=transport, runtime_root=runtime_root,
                              allowlist=allowlist, on_rejected=on_rejected, bot_open_id=bot_open_id, backoff=True,
                              channel_id=channel_id, manager_id=manager_id, manager_running=manager_running,
                              resolve_chat=resolve_chat, stop_chat=stop_chat)
    return adapter
