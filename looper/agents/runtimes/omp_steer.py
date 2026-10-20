"""One OMP run over its line-delimited rpc protocol, with acknowledged steering."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..claude_cli import iter_lines
from .omp import OmpStream, provider_error, safe_error

logger = logging.getLogger(__name__)
_STEER_TIMEOUT_SEC = 4.0
_DROPPED = frozenset({"ready", "session_settled", "available_commands_update",
                      "extension_ui_request"})


class OmpChannel:
    """The Worker's first prompt and further messages share one rpc process until EOF."""

    def __init__(self, process: asyncio.subprocess.Process, *, model: str | None, cwd: str,
                 secrets: tuple[str, ...] = ()) -> None:
        self._process = process
        self._stream = OmpStream(model=model, secrets=secrets)
        self._cwd = cwd
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._startup: dict[str, str] = {}
        self._prompt = ""
        self._setup_acked: set[str] = set()  # the setup requests acknowledged so far; the prompt needs both
        self._prompt_acknowledged = False
        self._ended = False
        self._result_seen = False

    async def _write(self, command: str, **params: Any) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        assert self._process.stdin is not None
        self._next_id += 1
        request_id = str(self._next_id)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._process.stdin.write((json.dumps({"id": request_id, "type": command, **params}) + "\n").encode())
            await self._process.stdin.drain()
        except BaseException:
            self._pending.pop(request_id, None)
            future.cancel()
            raise
        return request_id, future

    async def start(self, prompt: str) -> None:
        # The prompt follows only after both setup requests succeeded (``events``), so a refused interrupt mode
        # fails the launch before the model sees anything.
        self._prompt = prompt
        async with self._lock:
            for command, params in (("set_interrupt_mode", {"mode": "wait"}), ("get_state", {})):
                request_id, _ = await self._write(command, **params)
                self._startup[request_id] = command

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self._process.stdout is not None
        pending_failure: dict[str, Any] | None = None
        try:
            async for line in iter_lines(self._process.stdout):
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    logger.debug("OMP rpc: skipping a non-JSON line (%d bytes)", len(line))
                    continue
                if not isinstance(raw, dict):
                    logger.debug("OMP rpc: skipping a non-object line (%d bytes)", len(line))
                    continue
                if raw.get("type") == "response":
                    request_id = raw.get("id")
                    future = self._pending.pop(request_id, None) if isinstance(request_id, str) else None
                    if future is None:
                        if raw.get("command") == "parse" and not raw.get("success"):
                            logger.debug("OMP rpc: a command failed to parse")
                        continue
                    if not future.done():
                        future.set_result(raw)
                    command = self._startup.pop(request_id, None)
                    if command is not None:
                        if not raw.get("success"):
                            yield self._failure(str(raw.get("error") or f"{command} failed"))
                            return
                        if command == "get_state":
                            data = raw.get("data")
                            session_id = data.get("sessionId") if isinstance(data, dict) else None
                            for event in self._stream.translate({"type": "session", "id": session_id,
                                                                 "cwd": self._cwd}):
                                yield event
                        if command in ("set_interrupt_mode", "get_state"):
                            self._setup_acked.add(command)
                            if len(self._setup_acked) == 2:
                                async with self._lock:
                                    request_id, _ = await self._write("prompt", message=self._prompt)
                                    self._startup[request_id] = "prompt"
                        elif command == "prompt":
                            self._prompt_acknowledged = True
                    continue
                if raw.get("type") == "prompt_result":
                    error = raw.get("error")
                    # RPC's detailed prompt error arrives after agent_end. Publish one failure with that
                    # detail, instead of closing input as soon as the generic agent_end error arrives.
                    if (raw.get("status") == "error" and isinstance(error, dict)
                            and (pending_failure is not None or not self._result_seen)):
                        yield self._failure(provider_error(error.get("message"), error.get("httpStatus")),
                                            result=pending_failure)
                        pending_failure = None
                    if pending_failure is not None:
                        yield pending_failure
                        pending_failure = None
                    continue
                if raw.get("type") in _DROPPED:
                    continue
                for event in self._stream.translate(raw):
                    if event.get("type") == "result":
                        self._result_seen = True
                        if event.get("is_error"):
                            pending_failure = event
                            continue
                    yield event
            if pending_failure is not None:
                yield pending_failure
            if not self._prompt_acknowledged and not self._result_seen:
                yield self._failure("the process ended before acknowledging the prompt")
        finally:
            self._ended = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionResetError("the OMP rpc connection ended"))
                    # Startup requests have no waiter; send still observes the exception when awaited.
                    future.exception()
            self._pending.clear()

    def _failure(self, message: str, *, result: dict[str, Any] | None = None) -> dict[str, Any]:
        self._result_seen = True
        # Keep the terminal event's session and accumulated usage when RPC adds the error detail later.
        return {**(result if result is not None else self._stream._result()),
                "type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
                "errors": [safe_error(f"OMP rpc: {message}", secrets=self._stream.secrets, limit=None)]}

    async def send(self, text: str, *, uuid: str | None = None) -> None:
        try:
            response = await asyncio.wait_for(self._steer(text), _STEER_TIMEOUT_SEC)
        except TimeoutError:
            raise RuntimeError("OMP did not accept the steer in time") from None
        if not response.get("success"):
            raise RuntimeError(response.get("error") or "OMP rejected the steer")

    async def _steer(self, text: str) -> dict[str, Any]:
        """Lock, write and acknowledgement under the caller's one deadline."""
        assert self._process.stdin is not None
        async with self._lock:
            if self._ended or self._result_seen or self._process.stdin.is_closing():
                raise RuntimeError("the OMP run has ended")
            request_id, future = await self._write("steer", message=text)
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def end_input(self) -> None:
        assert self._process.stdin is not None
        if not self._process.stdin.is_closing():
            self._process.stdin.close()
