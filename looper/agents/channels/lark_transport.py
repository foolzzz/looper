"""The Lark adapter's send and receive boundary, including the lark-cli transport."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from . import lark_cli
from .lark_content import ResourceRef

logger = logging.getLogger(__name__)
_CONSUMER_STDERR_LINES = 5
_CONSUMER_STOP_GRACE_SEC = 5.0


class LarkTransport(Protocol):
    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None, return_id: bool = False) -> str | None: ...
    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None, return_id: bool = False) -> str | None: ...
    async def edit(self, message_id: str, *, text: str) -> None: ...
    async def react(self, message_id: str) -> None: ...
    async def download(self, message_id: str, ref: ResourceRef, part: Path, *, max_bytes: int) -> None: ...
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def names(self, open_ids: Iterable[str]) -> dict[str, str]: ...
    def available(self) -> bool: ...
    async def aclose(self) -> None: ...


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

    def __init__(self, profile: str = "") -> None:
        self._profile = profile
        self.consumer_started = asyncio.Event()
        self._history_chat_types: dict[str, str] = {}

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
        page = result.get("data", result)
        if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
            raise lark_cli.LarkCliError("history did not return a message page")
        return {**page, "messages": await self._history_references(page["messages"]), "chat_type": chat_type}

    async def _history_references(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched = []
        for message in messages:
            message = dict(message)
            if message.get("thread_id") and not (
                (message.get("reply_to") or message.get("parent_id")) and message.get("root_id")
            ):
                # Shortcuts discard parent/root ids. Read the exact message before
                # admission so a recovered gate reply cannot become a Manager turn.
                result = await lark_cli.run_json([
                    *lark_cli._base(self._profile), "api", "GET",
                    f"/open-apis/im/v1/messages/{quote(message['message_id'], safe='')}", "--as", "bot"])
                items = (result.get("data") or {}).get("items") or []
                raw = next((item for item in items if item.get("message_id") == message["message_id"]), None)
                if raw is None:
                    raise lark_cli.LarkCliError("history reply references are unavailable")
                message.update(parent_id=raw.get("parent_id") or "", root_id=raw.get("root_id") or "")
            if message.get("thread_replies"):
                message["thread_replies"] = await self._history_references(message["thread_replies"])
            enriched.append(message)
        return enriched

    async def list_threads(self, chat_id: str, *, page_token: str = "") -> dict[str, Any]:
        if await self._history_chat_type(chat_id) == "p2p":
            return {"threads": [], "has_more": False}
        result = await lark_cli.run_json(lark_cli.thread_roots_command(
            chat_id, page_token=page_token, profile=self._profile))
        page = result.get("data", result)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise lark_cli.LarkCliError("history did not return a thread root page")
        return {**page, "threads": list(dict.fromkeys(item["thread_id"] for item in page["items"]
                                                    if item.get("thread_id")))}

    async def list_recent_messages(self, chat_id: str, *, start: float, end: float,
                                   page_token: str = "") -> dict[str, Any]:
        chat_type = await self._history_chat_type(chat_id)
        if chat_type == "p2p":
            return {"messages": [], "chat_type": chat_type, "has_more": False}
        result = await lark_cli.run_json(lark_cli.recent_messages_command(
            chat_id, start=start, end=end, page_token=page_token, profile=self._profile))
        page = result.get("data", result)
        if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
            raise lark_cli.LarkCliError("history search did not return a message page")
        return {**page, "messages": await self._history_references(page["messages"]), "chat_type": chat_type}

    async def list_thread_messages(self, thread_id: str, *, page_token: str = "") -> dict[str, Any]:
        result = await lark_cli.run_json(lark_cli.thread_history_command(
            thread_id, page_token=page_token, profile=self._profile))
        page = result.get("data", result)
        if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
            raise lark_cli.LarkCliError("history did not return a thread message page")
        return {**page, "messages": await self._history_references(page["messages"]), "chat_type": "group"}

    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        return await self._message(lark_cli.send_command(
            chat_id=chat_id, user_id=user_id, text=text, markdown=markdown, profile=self._profile,
            idempotency_key=idempotency_key), return_id=return_id)

    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        return await self._message(lark_cli.reply_command(
            message_id, text=text, markdown=markdown, profile=self._profile, idempotency_key=idempotency_key),
            return_id=return_id)

    async def _message(self, argv: list[str], *, return_id: bool) -> str | None:
        """Only posts and status messages read the id from successful CLI output."""
        if return_id:
            return lark_cli.sent_message_id(await lark_cli.run_json(argv))
        await lark_cli.run(argv)
        return None

    async def edit(self, message_id: str, *, text: str) -> None:
        await lark_cli.run(lark_cli.edit_command(message_id, text=text, profile=self._profile))

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

