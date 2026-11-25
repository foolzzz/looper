"""The Lark adapter's send and receive boundary, including the lark-cli transport."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any, Awaitable, Protocol
from enum import Enum
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from . import lark_cli
from .lark_content import ResourceRef

logger = logging.getLogger(__name__)
_CONSUMER_STDERR_LINES = 5
_CONSUMER_STOP_GRACE_SEC = 5.0
# A message read's first try; the rest of its time goes to the second. Measured through lark-cli: a single read
# takes 4–5 s (about 1.5 s of it lark-cli's own start and sign-in, the rest the platform's), the batch read of
# the same message about 3 s; on a long merged forward the platform sometimes gives up after about 5 s.
READ_FIRST_SECONDS = 7.0


class LarkTransportKind(str, Enum):
    CLI = "cli"
    BOT = "bot"


class LarkTransport(Protocol):
    kind: LarkTransportKind
    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None, return_id: bool = False) -> str | None: ...
    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None, return_id: bool = False,
                    reply_in_thread: bool = False) -> str | None: ...
    async def edit(self, message_id: str, *, text: str | None = None, markdown: str | None = None) -> None: ...
    async def react(self, message_id: str) -> None: ...
    async def download(self, message_id: str, ref: ResourceRef, part: Path, *, max_bytes: int) -> None: ...
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def names(self, open_ids: Iterable[str]) -> dict[str, str]: ...
    def available(self) -> bool: ...
    async def aclose(self) -> None: ...


# All protocol writes require explicit wrappers. Forward only reviewed reads/receive state, so a newly added
# transport write cannot silently bypass the gateway even before it is added to the protocol.
LARK_WRITE_METHODS: frozenset[str] = frozenset({'send', 'reply', 'edit', 'react'})
_LARK_READ_ATTRIBUTES = frozenset({'download', 'events', 'names', 'available', 'consumer_started',
                                 'get_message', 'list_messages', 'list_threads', 'list_thread_messages',
                                 'chat_mode'})  # card authorization checks whether the source chat is direct


def _batch_items(batch: asyncio.Future[dict[str, Any]], message_id: str) -> list[dict[str, Any]] | None:
    """The batch read's answer when it stands for the single read's: the message alone, not recalled (the single
    read says why), and neither a merged forward nor a card; else None."""
    if not batch.done() or batch.cancelled() or batch.exception() is not None:
        return None
    data = batch.result().get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(item := items[0], dict):
        return None
    if item.get("message_id") != message_id or item.get("deleted") or item.get("msg_type") in {
            "merge_forward", "interactive"}:
        return None
    return items


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
    await process.wait()


class CliTransport:
    """Send through lark-cli and consume one event subprocess per connection."""

    kind = LarkTransportKind.CLI

    def __init__(self, profile: str = "") -> None:
        self._profile = profile
        self.consumer_started = asyncio.Event()
        self._history_chat_types: dict[str, str] = {}
        self.on_thread_activity: Callable[[str, str], Awaitable[None]] | None = None

    async def _history_chat_type(self, chat_id: str) -> str:
        # History omits chat mode. Never infer p2p from a missing mention: that would admit group chatter.
        if chat_id not in self._history_chat_types:
            info = await lark_cli.run_json([
                *lark_cli._base(self._profile), "api", "GET", f"/open-apis/im/v1/chats/{chat_id}", "--as", "bot"])
            mode = (info.get("data") or {}).get("chat_mode")
            if mode not in {"group", "p2p", "topic"}:
                raise lark_cli.LarkCliError("history chat mode is unavailable to the bot")
            self._history_chat_types[chat_id] = "p2p" if mode == "p2p" else "group"
        return self._history_chat_types[chat_id]

    async def list_messages(self, chat_id: str, *, start: float, end: float,
                            page_token: str = "") -> dict[str, Any]:
        chat_type = await self._history_chat_type(chat_id)
        result = await lark_cli.run_json(lark_cli.history_command(
            chat_id, start=start, end=end, page_token=page_token, profile=self._profile))
        return {**self._history_page(result), "chat_type": chat_type}

    @staticmethod
    def _history_page(result: dict[str, Any]) -> dict[str, Any]:
        page = result.get("data", result)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise lark_cli.LarkCliError("history did not return a message page")
        return {**page, "messages": [CliTransport._history_message(message) for message in page["items"]]}

    @staticmethod
    def _history_message(message: dict[str, Any]) -> dict[str, Any]:
        # Use the live renderer while preserving the raw history routing fields.
        from .lark_stream import _render

        kind = message["msg_type"]
        mentions = [{**mention, "key": mention.get("key") or "", "name": mention.get("name") or ""}
                    for mention in message.get("mentions") or []]
        content = (message.get("body") or {}).get("content") or ""
        if kind == "text":
            try:
                body = json.loads(content) if content else {}
            except (ValueError, TypeError):
                body = {}
            content = body.get("text") if isinstance(body, dict) else ""
            content = content if isinstance(content, str) else ""
            for mention in sorted(mentions, key=lambda item: len(item["key"]), reverse=True):
                if mention["key"]:
                    content = content.replace(mention["key"], "@" + mention["name"])
        else:
            content = _render(kind, content, mentions)
        return {**message, "content": content, "mentions": mentions,
                "thread_replies": [CliTransport._history_message(reply)
                                   for reply in message.get("thread_replies") or []]}

    async def list_threads(self, chat_id: str, *, page_token: str = "") -> dict[str, Any]:
        if await self._history_chat_type(chat_id) == "p2p":
            return {"roots": [], "has_more": False}
        result = await lark_cli.run_json(lark_cli.thread_roots_command(
            chat_id, page_token=page_token, profile=self._profile))
        page = result.get("data", result)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise lark_cli.LarkCliError("history did not return a thread root page")
        # Keep every root's time, including roots without a thread, for the age cutoff.
        return {**page, "roots": page["items"][:50]}

    async def list_thread_messages(self, thread_id: str, *, page_token: str = "") -> dict[str, Any]:
        result = await lark_cli.run_json(lark_cli.thread_history_command(
            thread_id, page_token=page_token, profile=self._profile))
        return {**self._history_page(result), "chat_type": "group"}

    async def get_message(self, message_id: str, *, timeout: float) -> list[dict[str, Any]]:
        """The message's raw items as the bot reads it: a merged forward first, then its children (each naming it in
        ``upper_message_id``). The batch read runs beside that read and answers instead when it returns the
        message, not recalled, and it is neither a merged forward (whose children only the single read returns) nor
        a card; otherwise the single read answers (``_read_message``)."""
        single = asyncio.ensure_future(self._read_message(message_id, timeout=timeout))
        batch = asyncio.ensure_future(lark_cli.run_json(lark_cli.batch_message_command(
            message_id, profile=self._profile), timeout=min(timeout, READ_FIRST_SECONDS)))
        try:
            await asyncio.wait((single, batch), return_when=asyncio.FIRST_COMPLETED)
            if not single.done() and (items := _batch_items(batch, message_id)) is not None:
                return items
            return await single
        finally:
            for task in (single, batch):
                task.cancel()  # lark-cli's process is killed (``lark_cli.run``)
            await asyncio.wait((single, batch))
            for task in (single, batch):
                if not task.cancelled():
                    task.exception()  # retrieved: the other read's failure is not the answer

    async def _read_message(self, message_id: str, *, timeout: float) -> list[dict[str, Any]]:
        """The single read (``GET /open-apis/im/v1/messages/{id}``). When it fails or times out, the rest of
        ``timeout`` goes to ``+messages-mget``, whose item has lark-cli's rendering (``rendered``: a merged forward
        as one block) and the sender's ``name``; when that fails too, the first failure is raised."""
        started = time.monotonic()
        try:
            answer = await lark_cli.run_json(lark_cli.message_command(message_id, profile=self._profile),
                                             timeout=min(timeout, READ_FIRST_SECONDS))
            items = answer.get("data", answer).get("items")
            if not isinstance(items, list):
                raise lark_cli.LarkCliError("the message read returned no items")
            return items
        except lark_cli.LarkCliError as first:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise
            try:
                answer = await lark_cli.run_json(lark_cli.mget_command(message_id, profile=self._profile),
                                                 timeout=remaining)
            except lark_cli.LarkCliError:
                raise first from None
            messages = (answer.get("data") or {}).get("messages") if isinstance(answer.get("data"), dict) else None
            if not isinstance(messages, list):
                raise first from None
            return [{"message_id": item.get("message_id") or "", "msg_type": item.get("msg_type") or "",
                     "create_time": item.get("create_time") or "", "sender": item.get("sender") or {},
                     "deleted": bool(item.get("deleted")), "rendered": str(item.get("content") or "")}
                    for item in messages if isinstance(item, dict)]

    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        try:
            argv = lark_cli.send_command(
                chat_id=chat_id, user_id=user_id, text=text, markdown=markdown, profile=self._profile,
                idempotency_key=idempotency_key)
        except ValueError as exc:
            raise lark_cli.LarkCliError(str(exc), not_sent=True) from exc
        return await self._message(argv, return_id=return_id, resolve_thread=chat_id is not None)

    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None, return_id: bool = False,
                    reply_in_thread: bool = False) -> str | None:
        try:
            argv = lark_cli.reply_command(
                message_id, text=text, markdown=markdown, profile=self._profile,
                idempotency_key=idempotency_key, reply_in_thread=reply_in_thread)
        except ValueError as exc:
            raise lark_cli.LarkCliError(str(exc), not_sent=True) from exc
        return await self._message(argv, return_id=return_id, resolve_thread=True)

    async def _message(self, argv: list[str], *, return_id: bool, resolve_thread: bool = False) -> str | None:
        """Remember successful thread sends without turning a bookkeeping error into a send retry."""
        if return_id or self.on_thread_activity is not None:
            answer = await lark_cli.run_json(argv)
            if self.on_thread_activity is not None:
                try:
                    data = answer.get("data", answer)
                    # Some shortcut versions return only the sent id and chat, omitting thread metadata.
                    if (resolve_thread and not (data.get("thread_id") and data.get("chat_id"))
                            and (sent_id := lark_cli.sent_message_id(answer))):
                        result = await lark_cli.run_json(lark_cli.message_command(sent_id, profile=self._profile))
                        items = result.get("data", result).get("items") or []
                        data = items[0] if items else {}
                    if data.get("chat_id") and data.get("thread_id"):
                        await self.on_thread_activity(data["chat_id"], data["thread_id"])
                except Exception:
                    logger.warning("could not remember sent Lark thread", exc_info=True)
            return lark_cli.sent_message_id(answer) if return_id else None
        await lark_cli.run(argv)
        return None

    async def edit(self, message_id: str, *, text: str | None = None, markdown: str | None = None) -> None:
        await lark_cli.run(lark_cli.edit_command(message_id, text=text, markdown=markdown, profile=self._profile))

    async def react(self, message_id: str) -> None:
        await lark_cli.run(lark_cli.reaction_command(message_id, profile=self._profile))

    async def download(self, message_id: str, ref: ResourceRef, part: Path, *, max_bytes: int) -> None:
        answer = await lark_cli.run_json(lark_cli.resource_command(
            message_id, ref.key, ref.kind.value, part.name, profile=self._profile), timeout=30, cwd=part.parent)
        data = answer.get("data")
        saved = data.get("saved_path") if isinstance(data, dict) else None
        if not isinstance(saved, str) or not saved:
            raise lark_cli.LarkCliError("lark-cli did not say where it saved the resource")
        path = (part.parent / saved).resolve()
        if path != part.resolve():
            if path.is_relative_to(part.parent.resolve()) and path.is_file():
                path.unlink()
            raise lark_cli.LarkCliError("lark-cli saved the resource somewhere else")

    async def names(self, open_ids: Iterable[str]) -> dict[str, str]:
        return await lark_cli.names(open_ids, self._profile or None)

    def available(self) -> bool:
        from .lark import _check_lark_cli_available

        return _check_lark_cli_available()

    async def aclose(self) -> None:
        self._history_chat_types.clear()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Keep stdin open until cancellation, then let lark-cli end its subscription."""
        self.consumer_started.clear()
        process = await asyncio.create_subprocess_exec(
            *lark_cli.consume_command(self._profile), env=lark_cli.environment(),
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
                    if text == f"[event] ready event_key={lark_cli.RECEIVE_EVENT}":
                        self.consumer_started.set()
                    stderr_tail.append(text[:300])

        stderr_task = asyncio.create_task(_read_stderr())
        stopped = False
        try:
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    # Older CLIs may omit the ready marker; a live event also proves subscription.
                    if isinstance(event, dict) and event.get("type") == lark_cli.RECEIVE_EVENT:
                        self.consumer_started.set()
                    yield event
                except Exception:
                    logger.debug("skipping non-JSON lark event line")
        except (asyncio.CancelledError, GeneratorExit):
            stopped = True
            raise
        finally:
            self.consumer_started.clear()
            await _stop_consumer(process)
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            if not stopped and stderr_tail:
                logger.warning("lark event consumer stopped (exit %s); its last output: %s",
                               process.returncode, " | ".join(stderr_tail))



@dataclass
class _MessageScope:
    message_id: str
    source: str = "answer"
    chat: str | None = None
    durable: bool = True
    part: int = 0


class OutboundDeliveryError(lark_cli.LarkCliError):
    """Keep the outbox receipt's refusal/unknown distinction for existing callers."""

    def __init__(self, receipt: Any) -> None:
        from .base import PostOutcome

        super().__init__(receipt.error or "outbound delivery is still pending",
                         code=230011 if receipt.parent_gone else None)
        self._rejected = receipt.result is PostOutcome.REJECTED

    @property
    def refused(self) -> bool:
        return self._rejected


class GatedLarkTransport:
    """One switch for both Lark transports; reads keep the inner transport's behavior."""

    def __init__(self, inner: LarkTransport, gateway: Any, *, enabled: Callable[[], bool],
                 outbox: Any = None) -> None:
        self.inner = inner
        self.gateway = gateway
        self.outbox = outbox
        self._enabled = enabled
        self._scope: ContextVar[_MessageScope | None] = ContextVar("lark_outbound_scope", default=None)

    @property
    def kind(self) -> LarkTransportKind | None:
        return getattr(self.inner, "kind", None)

    def __getattr__(self, name: str) -> Any:
        if name in LARK_WRITE_METHODS or name not in _LARK_READ_ATTRIBUTES:
            raise AttributeError(f"{type(self).__name__} has no gated method {name!r}")
        return getattr(self.inner, name)

    @contextmanager
    def message_scope(self, message_id: str, *, source: str = "answer", chat: str | None = None,
                      durable: bool = True):
        token = self._scope.set(_MessageScope(message_id, source, chat, durable))
        try:
            yield
        finally:
            self._scope.reset(token)

    async def _message(self, kind: str, body: dict[str, Any], reply_to: str | None = None) -> str | None:
        from .outbound import derived_key

        if body.get("idempotency_key") is not None:
            try:
                body["idempotency_key"] = lark_cli.normalize_key(body["idempotency_key"])
            except ValueError as exc:
                raise lark_cli.LarkCliError(str(exc), not_sent=True) from exc

        async def direct():
            if reply_to is not None:
                return await self.inner.reply(reply_to, **body)
            return await self.inner.send(**body)

        if not self._enabled():
            return await direct()
        scope = self._scope.get()
        if scope is not None and not scope.durable:
            body["idempotency_key"] = body.get("idempotency_key") or derived_key(
                self.gateway.channel_id, f"status:{scope.message_id}", scope.part)
            scope.part += 1
            return await self.gateway.write(kind, direct, keyed=True)
        if self.outbox is None:
            return await self.gateway.write(kind, direct, keyed=bool(body.get("idempotency_key")))
        scope = scope or _MessageScope(str(uuid4()))
        key = body.get("idempotency_key") or derived_key(self.gateway.channel_id, scope.message_id, scope.part)
        scope.part += 1
        chat = scope.chat or body.get("chat_id") or body.get("user_id") or reply_to
        payload = {k: v for k, v in body.items() if k not in {"idempotency_key", "return_id"} and v is not None}
        receipt = await self.outbox.send(kind, chat, payload, reply_to, key, scope.source)
        if not receipt.success:
            raise OutboundDeliveryError(receipt)
        return receipt.external_ids[0] if body.get("return_id") and receipt.external_ids else None

    async def send(self, **kwargs: Any) -> str | None:
        return await self._message("send", kwargs)

    async def reply(self, message_id: str, **kwargs: Any) -> str | None:
        return await self._message("reply", kwargs, message_id)

    async def edit(self, message_id: str, **kwargs: Any) -> None:
        if not self._enabled():
            return await self.inner.edit(message_id, **kwargs)
        await self.gateway.edit(message_id, lambda: self.inner.edit(message_id, **kwargs))

    async def aclose(self) -> None:
        await self.gateway.stop()
        await self.inner.aclose()

    async def react(self, message_id: str) -> None:
        if not self._enabled():
            return await self.inner.react(message_id)
        await self.gateway.write("react", lambda: self.inner.react(message_id))
