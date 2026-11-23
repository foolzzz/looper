"""The Lark bot-token transport and its channel adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .allowlist import SenderAllowlist
from .chat_routing import ChatResolver
from .lark import LarkChannelAdapter, RejectedListener
from .lark_bot_settings import stored_config
from .lark_cli import LarkCliError
from .lark_content import ResourceRef


from .lark_transport import LarkTransportKind


class BotTransport:
    """Use the open platform for messages and one deduplication set across connections."""

    kind = LarkTransportKind.BOT

    def __init__(self, app_id: str, app_secret: str, *, base_url: str | None = None,
                 admit: Callable[[dict[str, Any]], bool] | None = None) -> None:
        from .lark_api import LarkApi
        from .lark_stream import SeenEvents, StreamStats

        self._api = LarkApi(app_id, app_secret, **({"base_url": base_url} if base_url is not None else {}))
        self._seen = SeenEvents()
        self._admit = admit
        self._stats = StreamStats()
        # Answers a press of a card's button on the long connection (``LarkChannelAdapter._card_action``).
        self.on_card: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None

    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        return await self._api.send(chat_id=chat_id, user_id=user_id, text=text, markdown=markdown,
                                    idempotency_key=idempotency_key)

    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None, return_id: bool = False,
                    reply_in_thread: bool = False) -> str | None:
        return await self._api.reply(message_id, text=text, markdown=markdown, idempotency_key=idempotency_key,
                                     **({"reply_in_thread": True} if reply_in_thread else {}))

    async def edit(self, message_id: str, *, text: str | None = None, markdown: str | None = None) -> None:
        await self._api.edit(message_id, text=text, markdown=markdown)

    async def react(self, message_id: str) -> None:
        await self._api.react(message_id)

    async def send_card(self, card: dict[str, Any], *, chat_id: str | None = None, user_id: str | None = None,
                        idempotency_key: str | None = None) -> tuple[str | None, str | None]:
        return await self._api.send_card(card, chat_id=chat_id, user_id=user_id, idempotency_key=idempotency_key)

    async def reply_card(self, message_id: str, card: dict[str, Any], *, idempotency_key: str | None = None,
                         reply_in_thread: bool = False) -> tuple[str | None, str | None]:
        return await self._api.reply_card(message_id, card, idempotency_key=idempotency_key,
                                          reply_in_thread=reply_in_thread)

    async def chat_mode(self, chat_id: str) -> str | None:
        return await self._api.chat_mode(chat_id)

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        await self._api.update_card(message_id, card)

    async def download(self, message_id: str, ref: ResourceRef, part: Path, *, max_bytes: int) -> None:
        await self._api.download(message_id, ref.key, ref.kind.value, part, max_bytes=max_bytes)

    async def get_message(self, message_id: str, *, timeout: float) -> list[dict[str, Any]]:
        """The message's raw items (a merged forward first, then its children); read once more, with the rest of
        ``timeout``, when the first read fails or times out."""
        from .lark_transport import READ_FIRST_SECONDS

        started = time.monotonic()
        path = f"/open-apis/im/v1/messages/{quote(message_id, safe='')}"
        try:
            answer = await asyncio.wait_for(self._api.call("GET", path), min(timeout, READ_FIRST_SECONDS))
        except (TimeoutError, LarkCliError):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise
            answer = await asyncio.wait_for(self._api.call("GET", path), remaining)
        items = (answer.get("data") or {}).get("items")
        if not isinstance(items, list):
            raise LarkCliError("the message read returned no items")
        return items

    async def list_thread_messages(self, thread_id: str, *, page_token: str = "") -> dict[str, Any]:
        """A page of the thread's messages, newest first, as the CLI transport lists them."""
        from .lark_transport import CliTransport

        params: dict[str, Any] = {"container_id_type": "thread", "container_id": thread_id,
                                  "sort_type": "ByCreateTimeDesc", "page_size": 50}
        if page_token:
            params["page_token"] = page_token
        answer = await self._api.call("GET", "/open-apis/im/v1/messages", params=params)
        return {**CliTransport._history_page(answer), "chat_type": "group"}

    def events(self) -> AsyncIterator[dict[str, Any]]:
        from .lark_stream import stream_events

        return stream_events(self._api, seen=self._seen, admit=self._admit, stats=self._stats,
                             **({"on_card": self.on_card} if self.on_card is not None else {}))

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
        return sender_id in allowlist.ids or (decision is not None and (sender_id in decision.senders or (event.get("chat_type") == "group"
                    and decision.explicit and decision.sender_policy == "chat")))

    config = stored_config(config)
    transport = BotTransport(config["app_id"], app_secret, base_url=base_url or BASE_URLS[config["domain"]],
                             admit=admit)
    adapter = LarkChannelAdapter(channel_type="lark_bot", transport=transport, runtime_root=runtime_root,
                              allowlist=allowlist, on_rejected=on_rejected, bot_open_id=bot_open_id, bot_app_id=config["app_id"], backoff=True,
                              channel_id=channel_id, manager_id=manager_id, manager_running=manager_running,
                              resolve_chat=resolve_chat, stop_chat=stop_chat)
    transport.on_card = adapter._card_action
    return adapter
