"""The Lark adapter's send and receive boundary, including the lark-cli transport."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Iterable
from typing import Any, Protocol

from . import lark_cli

logger = logging.getLogger(__name__)
_CONSUMER_STDERR_LINES = 5
_CONSUMER_STOP_GRACE_SEC = 5.0


class LarkTransport(Protocol):
    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None) -> str | None: ...
    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None) -> str | None: ...
    async def edit(self, message_id: str, *, text: str) -> None: ...
    async def react(self, message_id: str) -> None: ...
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


class CliTransport:
    """Send through lark-cli and consume one event subprocess per connection."""

    def __init__(self, profile: str = "") -> None:
        self._profile = profile

    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None) -> str | None:
        return await self._message(lark_cli.send_command(
            chat_id=chat_id, user_id=user_id, text=text, markdown=markdown, profile=self._profile,
            idempotency_key=idempotency_key))

    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None) -> str | None:
        return await self._message(lark_cli.reply_command(
            message_id, text=text, markdown=markdown, profile=self._profile, idempotency_key=idempotency_key))

    async def _message(self, argv: list[str]) -> str | None:
        """A successful CLI send can have no output; posts still require an id."""
        out = await lark_cli.run(argv)
        if not out:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            raise lark_cli.LarkCliError(f"lark-cli printed something that is not JSON: {out[:200]!r}") from None
        if not isinstance(data, dict):
            raise lark_cli.LarkCliError("lark-cli printed JSON that is not an object")
        if data.get("ok") is False:
            raise lark_cli.LarkCliError(str((data.get("error") or {}).get("message") or "lark-cli answered ok: false"))
        return lark_cli.sent_message_id(data)

    async def edit(self, message_id: str, *, text: str) -> None:
        await lark_cli.run(lark_cli.edit_command(message_id, text=text, profile=self._profile))

    async def react(self, message_id: str) -> None:
        await lark_cli.run(lark_cli.reaction_command(message_id, profile=self._profile))

    async def names(self, open_ids: Iterable[str]) -> dict[str, str]:
        return await lark_cli.names(open_ids, self._profile or None)

    def available(self) -> bool:
        from .lark import _check_lark_cli_available

        return _check_lark_cli_available()

    async def aclose(self) -> None:
        pass

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Keep stdin open until cancellation, then let lark-cli end its subscription."""
        process = await asyncio.create_subprocess_exec(
            *lark_cli.consume_command(self._profile),
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
                    yield event
                except Exception:
                    logger.debug("skipping non-JSON lark event line")
        except (asyncio.CancelledError, GeneratorExit):
            stopped = True
            raise
        finally:
            await _stop_consumer(process)
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            if not stopped and stderr_tail:
                logger.warning("lark event consumer stopped (exit %s); its last output: %s",
                               process.returncode, " | ".join(stderr_tail))

